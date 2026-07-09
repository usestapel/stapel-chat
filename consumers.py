"""ChatConsumer — the WebSocket transport for conversation delivery.

Store-first, transport-thin (the studio-chat §2.4 discipline, an independent
generic implementation): the socket never owns state. New messages are written
by the service layer (REST or the ``send`` frame below), whose ``on_commit``
fan-out relays the committed row to the Channels group; the socket only carries
the durable, ``seq``-ordered journal, so a dropped socket loses nothing.

Correctness never depends on live delivery:

* **Ordered by seq + idempotent.** Every outgoing frame carries the monotonic
  per-conversation ``seq``; the consumer drops any frame whose ``seq`` it has
  already sent, so the replay-then-live overlap after a resume never
  double-delivers.
* **Reconnect by last_seq.** ``hello{last_seq}`` replays ``Message.seq >
  last_seq`` from the database, then ``replay_done``. A gap wider than the
  replay window answers ``error{code=resync}`` so the client re-hydrates via
  the REST history endpoint.

Protocol
--------
client → server:  ``hello{last_seq}`` / ``send{body,attachments,reply_to}`` /
                  ``ack{seq}`` / ``ping``
server → client:  ``welcome{server_seq}`` / ``message{…seq}`` /
                  ``replay_done{up_to_seq}`` / ``error{code,message}`` / ``pong``

Channels is an optional extra. Importing this module without it raises a clear
ImportError; it is never imported at app-ready time (the package works
HTTP-only). Wire it in the host's ``asgi.py`` behind
``stapel_core.django.jwt.channels.JWTAuthMiddlewareStack`` so
``scope["user"]`` is populated.
"""
from __future__ import annotations

import logging

try:
    from channels.db import database_sync_to_async
    from channels.generic.websocket import AsyncJsonWebsocketConsumer
except ImportError as exc:  # pragma: no cover - exercised via optional-dep test
    raise ImportError(
        "stapel_chat.consumers requires the optional 'channels' dependency. "
        "Install it with:\n    pip install 'stapel-chat[channels]'"
    ) from exc

logger = logging.getLogger(__name__)

#: Widest resume gap replayed inline before the client is told to re-hydrate via
#: the REST history endpoint.
REPLAY_LIMIT = 500


def _authorize_sync(conversation_id: str, user_id) -> bool:
    """A user may subscribe iff they are a participant of the conversation."""
    from django.core.exceptions import ValidationError

    from .models import ConversationParticipant

    try:
        return ConversationParticipant.objects.filter(
            conversation_id=conversation_id, user_id=user_id
        ).exists()
    except (ValidationError, ValueError):
        return False


def _server_seq_sync(conversation_id: str) -> int:
    from .models import Conversation

    conv = Conversation.objects.filter(pk=conversation_id).values("last_seq").first()
    return conv["last_seq"] if conv else 0


def _replay_rows_sync(conversation_id: str, after_seq: int, limit: int) -> list:
    from .models import Message
    from .realtime import message_frame

    rows = (
        Message.objects.filter(conversation_id=conversation_id, seq__gt=after_seq)
        .select_related("conversation")
        .order_by("seq")[:limit]
    )
    return [message_frame(m, m.conversation) for m in rows]


def _send_message_sync(conversation_id: str, user_id, content: dict) -> dict | None:
    """Persist an inbound ``send`` frame via the service layer. Returns an error
    dict on refusal, else None (the fan-out delivers the message frame)."""
    from django.contrib.auth import get_user_model

    from . import services
    from .conf import chat_settings
    from .models import Conversation, Message, MessageKind

    body = (content.get("body") or "").strip()
    attachments = content.get("attachments") or []
    if not body and not attachments:
        return {"code": "empty", "message": "message needs a body or an attachment"}
    if attachments and not chat_settings.ATTACHMENTS:
        return {"code": "attachments_disabled", "message": "attachments are disabled"}
    if len(body) > chat_settings.MAX_BODY_LENGTH:
        return {"code": "too_long", "message": "message body too long"}

    conv = Conversation.objects.filter(pk=conversation_id).first()
    if conv is None:
        return {"code": "not_found", "message": "conversation not found"}
    User = get_user_model()
    sender = User.objects.get(pk=user_id)
    reply_to = None
    reply_to_id = content.get("reply_to")
    if reply_to_id:
        reply_to = Message.objects.filter(
            pk=reply_to_id, conversation_id=conversation_id
        ).first()
        if reply_to is None:
            return {"code": "invalid_reply", "message": "reply target not in thread"}
    services.post_message(
        conversation=conv, sender=sender, body=body,
        attachments=list(attachments), reply_to=reply_to, kind=MessageKind.TEXT,
    )
    return None


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """One socket ↔ one conversation group. Auth on connect, protocol in receive."""

    async def connect(self):
        self.conversation_id = str(
            self.scope["url_route"]["kwargs"]["conversation_id"]
        )
        user = self.scope.get("user")
        self.user_id = getattr(user, "id", None) or getattr(user, "pk", None)
        self._max_seq_sent = 0
        self.acked_seq = 0
        if not self.user_id:
            await self.close(code=4401)  # unauthenticated
            return
        allowed = await database_sync_to_async(_authorize_sync)(
            self.conversation_id, self.user_id
        )
        if not allowed:
            await self.close(code=4403)  # not a participant
            return
        self.group = _group_name(self.conversation_id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if getattr(self, "group", None):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    # ── inbound (client → server) ────────────────────────────────────────

    async def receive_json(self, content, **kwargs):
        handler = {
            "hello": self._on_hello,
            "send": self._on_send,
            "ack": self._on_ack,
            "ping": self._on_ping,
        }.get(content.get("type"))
        if handler is None:
            await self._error("bad_type", f"unknown type {content.get('type')!r}")
            return
        await handler(content)

    async def _on_hello(self, content):
        last_seq = int(content.get("last_seq") or 0)
        server_seq = await database_sync_to_async(_server_seq_sync)(
            self.conversation_id
        )
        await self.send_json(
            {
                "type": "welcome",
                "conversation_id": self.conversation_id,
                "server_seq": server_seq,
            }
        )
        # Advance our dedup cursor to what the client already has, so a
        # concurrent live frame at last_seq is not re-sent.
        self._max_seq_sent = max(self._max_seq_sent, last_seq)
        if server_seq - last_seq > REPLAY_LIMIT:
            await self._error(
                "resync",
                f"resume gap {server_seq - last_seq} exceeds window {REPLAY_LIMIT}",
            )
            return
        rows = await database_sync_to_async(_replay_rows_sync)(
            self.conversation_id, last_seq, REPLAY_LIMIT
        )
        for frame in rows:
            await self._send_frame(frame)
        await self.send_json({"type": "replay_done", "up_to_seq": server_seq})

    async def _on_send(self, content):
        err = await database_sync_to_async(_send_message_sync)(
            self.conversation_id, self.user_id, content
        )
        if err:
            await self._error(err["code"], err["message"])
        # On success the on_commit fan-out delivers the message frame back to
        # this socket (and every subscriber) — no direct echo.

    async def _on_ack(self, content):
        self.acked_seq = max(self.acked_seq, int(content.get("seq") or 0))

    async def _on_ping(self, content):
        await self.send_json({"type": "pong"})

    # ── outbound (server → client) ───────────────────────────────────────

    async def chat_frame(self, event):
        """Channels group event → socket (seq-dedup lives in _send_frame)."""
        await self._send_frame(event["frame"])

    async def _send_frame(self, frame: dict):
        seq = frame.get("seq")
        if seq is not None and seq <= self._max_seq_sent:
            return  # idempotent by seq — drop replays of what we already sent
        await self.send_json(frame)
        if seq is not None:
            self._max_seq_sent = max(self._max_seq_sent, seq)

    async def _error(self, code: str, message: str, **extra):
        await self.send_json(
            {"type": "error", "code": code, "message": message, **extra}
        )


def _group_name(conversation_id: str) -> str:
    from .realtime import group_name

    return group_name(conversation_id)
