"""The two chat sockets. Realtime is the path, not an option on it.

Until 0.3.0 this file was chat's *own* WebSocket implementation — its own
frame shapes, its own close codes, its own resume loop. ``stapel-realtime``
generalized exactly that protocol into a substrate, and its module map names
chat as one of the three duplicate implementations it exists to end. So this
file no longer implements a socket; it implements the two hooks a resumable
stream needs, plus the frames chat adds on top.

Two consumers, one per stream (:mod:`stapel_chat.realtime`):

* :class:`ChatConsumer` — ``chat:conv:<id>``. Resumable, and the canonical
  place a message is both sent and received. Replay is anchored on
  ``rev_seq``, so an edit or a delete that happened while the client was away
  arrives in the catch-up like any other change.
* :class:`ChatInboxConsumer` — ``chat:user:<id>``. Ephemeral. It exists
  because a conversation *list* that has no socket is a conversation list
  that polls, and a chat which polls its inbox is a polling chat however live
  the open thread is.

**Frame sequence is not message sequence.** The envelope's ``seq`` is the
journal cursor (``rev_seq``); the payload's ``seq`` is the message's position
in the thread. A client upserts by ``message_id``, orders by the payload's
``seq``, and remembers the envelope's ``seq`` as its resume cursor. Getting
this backwards makes edits reorder the thread.

Writes over the socket
----------------------
The substrate's default posture is that writes go over REST. Chat is the
documented exception and it is a deliberate one: the owner's ruling is that a
chat client gets a full WebSocket, and a compose box whose Enter key takes a
different transport than the messages it produces is the seam where "realtime
was built" stops being true. Every write frame here goes through the same
service layer the REST view calls — one validation path, one emit, one
fan-out — and carries a ``client_msg_id`` so a retry after a dropped socket
is idempotent rather than a duplicate bubble.

Channels is an optional extra of ``stapel-realtime``. Importing this module
without it raises a clear ImportError; it is never imported at app-ready time.
"""
from __future__ import annotations

import logging

try:
    from channels.db import database_sync_to_async
except ImportError as exc:  # pragma: no cover - exercised via optional-dep test
    raise ImportError(
        "stapel_chat.consumers requires the optional 'channels' dependency. "
        "Install it with:\n    pip install 'stapel-chat[realtime]'"
    ) from exc

from stapel_realtime import envelope as wire
from stapel_realtime.consumers import (
    EphemeralStreamConsumer,
    JournalRow,
    ResumableStreamConsumer,
)

from .realtime import conversation_stream, message_payload, user_stream

logger = logging.getLogger(__name__)

# ── client → server frame types chat adds to the substrate's three ───────

SEND = "send"
EDIT = "edit"
DELETE = "delete"
READ = "read"
DELIVERED = "delivered"
ACTIVITY = "activity"

#: ``error`` codes this module emits, on top of the substrate's.
ERROR_EMPTY = "empty"
ERROR_TOO_LONG = "too_long"
ERROR_ATTACHMENTS_DISABLED = "attachments_disabled"
ERROR_INVALID_ATTACHMENT = "invalid_attachment"
ERROR_UNKNOWN_ATTACHMENT_TYPE = "unknown_attachment_type"
ERROR_INVALID_REPLY = "invalid_reply"
ERROR_NOT_FOUND = "not_found"
ERROR_NOT_AUTHOR = "not_author"
ERROR_NOT_EDITABLE = "not_editable"
ERROR_DELETED = "deleted"
ERROR_UNKNOWN_ACTIVITY = "unknown_activity"
# A send refused because a block stands between the two parties. The frame
# carries no reason and no direction — the socket is the path a blocked sender
# actually uses, and a wire code that named the block would announce it.
ERROR_SEND_REFUSED = "send_refused"
# The block store is configured and could not be asked. Distinct from
# `send_refused` on purpose: a client may retry this one, and must not show
# the sender anything that reads as a rejection by the other party.
ERROR_UNAVAILABLE = "unavailable"


# ── sync helpers (each runs in a thread) ────────────────────────────────


def _is_participant(conversation_id, user_id) -> bool:
    from django.core.exceptions import ValidationError

    from .models import ConversationParticipant

    try:
        return ConversationParticipant.objects.filter(
            conversation_id=conversation_id, user_id=user_id
        ).exists()
    except (ValidationError, ValueError):
        return False


def _server_seq(conversation_id) -> int:
    from .models import Conversation

    row = Conversation.objects.filter(pk=conversation_id).values("last_seq").first()
    return row["last_seq"] if row else 0


def _replay(conversation_id, after_seq: int, limit: int) -> list:
    from . import services

    return [
        JournalRow(seq=m.rev_seq, payload=message_payload(m, m.conversation))
        for m in services.journal_rows(
            conversation_id=conversation_id, after_seq=after_seq, limit=limit
        )
    ]


def _conversation_and_user(conversation_id, user_id):
    from django.contrib.auth import get_user_model

    from .models import Conversation

    conv = Conversation.objects.filter(pk=conversation_id).first()
    user = get_user_model().objects.filter(pk=user_id).first()
    return conv, user


def _do_send(conversation_id, user_id, payload: dict) -> dict | None:
    """Persist an inbound ``send``. Returns an error dict, or None on success
    (the on-commit fan-out delivers the frame to every subscriber, sender
    included — there is no separate echo to keep in step)."""
    from . import services
    from .attachments import InvalidAttachment, UnknownAttachmentType
    from .conf import chat_settings
    from .models import Message, MessageKind

    body = (payload.get("body") or "").strip()
    attachments = payload.get("attachments") or []
    if not body and not attachments:
        return {"code": ERROR_EMPTY, "message": "message needs a body or an attachment"}
    if attachments and not chat_settings.ATTACHMENTS:
        return {
            "code": ERROR_ATTACHMENTS_DISABLED,
            "message": "attachments are disabled",
        }
    if len(body) > chat_settings.MAX_BODY_LENGTH:
        return {"code": ERROR_TOO_LONG, "message": "message body too long"}

    conv, sender = _conversation_and_user(conversation_id, user_id)
    if conv is None or sender is None:
        return {"code": ERROR_NOT_FOUND, "message": "conversation not found"}

    reply_to = None
    reply_to_id = payload.get("reply_to")
    if reply_to_id:
        reply_to = Message.objects.filter(
            pk=reply_to_id, conversation_id=conversation_id
        ).first()
        if reply_to is None:
            return {
                "code": ERROR_INVALID_REPLY,
                "message": "reply target not in thread",
            }
    try:
        services.post_message(
            conversation=conv,
            sender=sender,
            body=body,
            attachments=list(attachments),
            reply_to=reply_to,
            kind=MessageKind.TEXT,
            client_msg_id=str(payload.get("client_msg_id") or ""),
        )
    except services.SendRefused:
        return {"code": ERROR_SEND_REFUSED, "message": "message not sent"}
    except services.BlockCheckUnavailable:
        return {"code": ERROR_UNAVAILABLE, "message": "try again shortly"}
    except UnknownAttachmentType as exc:
        return {
            "code": ERROR_UNKNOWN_ATTACHMENT_TYPE,
            "message": f"unknown attachment type {exc}",
        }
    except InvalidAttachment as exc:
        return {"code": ERROR_INVALID_ATTACHMENT, "message": str(exc)}
    return None


def _do_edit(conversation_id, user_id, payload: dict) -> dict | None:
    from . import services
    from .conf import chat_settings
    from .models import Message

    body = (payload.get("body") or "").strip()
    if not body:
        return {"code": ERROR_EMPTY, "message": "an edited message needs a body"}
    if len(body) > chat_settings.MAX_BODY_LENGTH:
        return {"code": ERROR_TOO_LONG, "message": "message body too long"}
    msg = Message.objects.filter(
        pk=payload.get("message_id"), conversation_id=conversation_id
    ).first()
    if msg is None:
        return {"code": ERROR_NOT_FOUND, "message": "message not found"}
    _, editor = _conversation_and_user(conversation_id, user_id)
    try:
        services.edit_message(message=msg, editor=editor, body=body)
    except services.NotAuthor:
        return {"code": ERROR_NOT_AUTHOR, "message": "only the author may edit"}
    except services.MessageGone:
        return {"code": ERROR_DELETED, "message": "this message has been deleted"}
    except services.NotEditable as exc:
        return {"code": ERROR_NOT_EDITABLE, "message": str(exc)}
    return None


def _do_delete(conversation_id, user_id, payload: dict) -> dict | None:
    from . import services
    from .models import Message

    msg = Message.objects.filter(
        pk=payload.get("message_id"), conversation_id=conversation_id
    ).first()
    if msg is None:
        return {"code": ERROR_NOT_FOUND, "message": "message not found"}
    _, actor = _conversation_and_user(conversation_id, user_id)
    try:
        services.delete_message(message=msg, actor=actor)
    except services.NotAuthor:
        return {"code": ERROR_NOT_AUTHOR, "message": "only the author may delete"}
    return None


def _do_marker(conversation_id, user_id, payload: dict, *, read: bool) -> dict | None:
    from . import services

    try:
        upto = max(0, int(payload.get("upto_seq") or 0))
    except (TypeError, ValueError):
        return {"code": wire.ERROR_BAD_ENVELOPE, "message": "'upto_seq' must be an int"}
    conv, user = _conversation_and_user(conversation_id, user_id)
    if conv is None or user is None:
        return {"code": ERROR_NOT_FOUND, "message": "conversation not found"}
    if read:
        services.mark_read(conversation=conv, user=user, upto_seq=upto)
    else:
        services.mark_delivered(conversation=conv, user=user, upto_seq=upto)
    return None


def _do_activity(conversation_id, user_id, payload: dict) -> dict | None:
    from . import services
    from .activity import UnknownActivityState

    conv, user = _conversation_and_user(conversation_id, user_id)
    if conv is None or user is None:
        return {"code": ERROR_NOT_FOUND, "message": "conversation not found"}
    try:
        services.announce_activity(
            conversation=conv, user=user, state=str(payload.get("state") or "")
        )
    except UnknownActivityState as exc:
        return {
            "code": ERROR_UNKNOWN_ACTIVITY,
            "message": f"unknown activity state {exc}",
        }
    return None


def _presence_connect(user_id) -> None:
    from . import presence

    presence.on_connect(user_id)


def _presence_disconnect(user_id) -> None:
    from . import presence

    presence.on_disconnect(user_id)


def _presence_touch(user_id) -> None:
    from . import presence

    presence.touch(user_id)


# ── consumers ────────────────────────────────────────────────────────────


class PresenceMixin:
    """Turns a live socket into a fact about its user.

    Both chat sockets count — the thread and the inbox alike — because either
    one open is a person reachable. Presence is per USER, so two tabs on two
    threads are one online person and closing one of them changes nothing.

    Three events feed it:

    * **connect**, after the substrate accepted (``self.group`` is set only on
      the accepted path, which is what distinguishes it from the four early
      returns that close instead);
    * **disconnect**, unconditionally — including the closes the substrate
      makes on its own (heartbeat timeout, expired token), which is exactly
      when a peer most needs to stop being told somebody is there;
    * **every inbound frame**, as evidence of life. That includes the
      heartbeat's ``pong``, which is the cheapest liveness signal there is,
      so the lease renews without the client having to do anything. The
      in-process guard below keeps a pong from costing a thread hop, and
      :func:`presence.touch` keeps it from costing a write.

    A presence failure never touches the socket: the thread is the product and
    a header is not worth a disconnect.
    """

    #: Least seconds between two touches from THIS socket. A cheap local
    #: guard in front of the database-level throttle in `presence.touch` —
    #: the point is to skip the thread hop, not to be the authority.
    presence_touch_interval_s = 15.0

    async def connect(self):
        self._presence_touched_at = 0.0
        self._presence_counted = False
        await super().connect()
        if getattr(self, "group", None) is None:
            return  # closed before accept — nobody connected
        user_id = self._user_id()
        if user_id is None:
            return
        self._presence_counted = True
        try:
            await database_sync_to_async(_presence_connect)(user_id)
        except Exception:  # noqa: BLE001 — presence never closes a socket
            logger.warning("chat: presence connect failed", exc_info=True)

    async def disconnect(self, code):
        await super().disconnect(code)
        # Only decrement what was counted: an unaccepted socket never
        # incremented, and decrementing it would strand the user offline
        # while a real tab of theirs is open.
        if not getattr(self, "_presence_counted", False):
            return
        self._presence_counted = False
        user_id = self._user_id()
        if user_id is None:
            return
        try:
            await database_sync_to_async(_presence_disconnect)(user_id)
        except Exception:  # noqa: BLE001
            logger.warning("chat: presence disconnect failed", exc_info=True)

    async def receive_json(self, content, **kwargs):
        await self._presence_evidence()
        await super().receive_json(content, **kwargs)

    async def _presence_evidence(self) -> None:
        import time

        if not getattr(self, "_presence_counted", False):
            return
        now = time.monotonic()
        if now - getattr(self, "_presence_touched_at", 0.0) < (
            self.presence_touch_interval_s
        ):
            return
        self._presence_touched_at = now
        user_id = self._user_id()
        if user_id is None:
            return
        try:
            await database_sync_to_async(_presence_touch)(user_id)
        except Exception:  # noqa: BLE001
            logger.warning("chat: presence touch failed", exc_info=True)


class ChatConsumer(PresenceMixin, ResumableStreamConsumer):
    """One socket ↔ one conversation, resumable by ``rev_seq``.

    The substrate owns authentication, the envelope, the heartbeat, backpressure
    and revoke-to-kick. This class supplies the two journal hooks, the
    participation gate, and chat's six write frames.
    """

    module = "chat"
    scope_type = "conv"
    stream_key_kwarg = "conversation_id"

    async def get_stream_key(self) -> str:
        kwargs = (self.scope.get("url_route") or {}).get("kwargs") or {}
        self.conversation_id = str(kwargs["conversation_id"])
        return conversation_stream(self.conversation_id)

    async def authorize(self, scope, stream_key) -> bool:
        """A user may watch a conversation iff they are a participant of it.

        Not ``WorkspaceCapability``: a chat thread's read right is membership
        of that thread, not a capability in a workspace — a support customer
        typically holds no mandate anywhere and must still read their own
        ticket.
        """
        user_id = self._user_id()
        if user_id is None:
            return False
        return await database_sync_to_async(_is_participant)(
            self.conversation_id, user_id
        )

    # ── journal hooks ────────────────────────────────────────────────────

    async def get_server_seq(self) -> int:
        return await database_sync_to_async(_server_seq)(self.conversation_id)

    async def get_replay_rows(self, after_seq: int, limit: int):
        return await database_sync_to_async(_replay)(
            self.conversation_id, after_seq, limit
        )

    # ── chat's write frames ──────────────────────────────────────────────

    def frame_handlers(self):
        handlers = super().frame_handlers()
        handlers.update(
            {
                SEND: self.on_send,
                EDIT: self.on_edit,
                DELETE: self.on_delete,
                READ: self.on_read,
                DELIVERED: self.on_delivered,
                ACTIVITY: self.on_activity,
            }
        )
        return handlers

    async def _run(self, func, frame):
        err = await database_sync_to_async(func)(
            self.conversation_id, self._user_id(), frame.payload
        )
        if err:
            await self._error(err["code"], err["message"])

    async def on_send(self, frame):
        await self._run(_do_send, frame)

    async def on_edit(self, frame):
        await self._run(_do_edit, frame)

    async def on_delete(self, frame):
        await self._run(_do_delete, frame)

    async def on_read(self, frame):
        await self._run(
            lambda c, u, p: _do_marker(c, u, p, read=True), frame
        )

    async def on_delivered(self, frame):
        await self._run(
            lambda c, u, p: _do_marker(c, u, p, read=False), frame
        )

    async def on_activity(self, frame):
        await self._run(_do_activity, frame)


class ChatInboxConsumer(PresenceMixin, EphemeralStreamConsumer):
    """One socket ↔ one user's inbox. Read-only, ephemeral.

    Everything it carries — a new message in some thread, an unread count that
    moved — is recoverable by listing conversations over REST, which is
    exactly the substrate's condition for a fact being allowed to travel as a
    Signal. It exists so that recovery is not the *normal* path: without it a
    conversation list has no socket, and a list with no socket refreshes on a
    timer forever no matter how live the open thread is.
    """

    module = "chat"
    scope_type = "user"
    stream_key_kwarg = "user_id"

    async def get_stream_key(self) -> str:
        return user_stream(self._user_id())

    async def authorize(self, scope, stream_key) -> bool:
        """You may watch exactly one inbox: your own.

        The stream key is derived from the authenticated scope rather than
        read from the URL, so there is no id to tamper with — the route
        carries no user segment at all.
        """
        return self._user_id() is not None


__all__ = [
    "ACTIVITY",
    "ChatConsumer",
    "PresenceMixin",
    "ChatInboxConsumer",
    "DELETE",
    "DELIVERED",
    "EDIT",
    "READ",
    "SEND",
]
