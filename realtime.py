"""Realtime fan-out helper (Channels-optional).

The send path schedules :func:`broadcast_message` ``on_commit`` to push a new
message to every subscriber of the conversation's Channels group. This is
**best-effort**: if Channels is not installed, no channel layer is configured,
or the layer send fails, delivery is simply skipped — correctness is preserved
because a client recovers missed messages by replaying durable rows by ``seq``
(see :mod:`stapel_chat.consumers`). Nothing here is imported at app-ready time,
so an HTTP-only host never touches Channels.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def group_name(conversation_id) -> str:
    """Channels group every subscriber of a conversation joins."""
    return f"chat.conv.{conversation_id}"


def message_frame(msg, conv) -> dict:
    """server→client ``message`` frame for a persisted message (carries ``seq``
    so the consumer can dedup replays against live frames)."""
    return {
        "type": "message",
        "message_id": str(msg.id),
        "conversation_id": str(conv.id),
        "sender_id": str(msg.sender_id) if msg.sender_id else None,
        "seq": msg.seq,
        "kind": msg.kind,
        "body": msg.body,
        "reply_to": str(msg.reply_to_id) if msg.reply_to_id else None,
        "attachments": msg.attachments,
        "created_at": msg.created_at.isoformat(),
    }


def _channel_layer():
    try:
        from channels.layers import get_channel_layer
    except ImportError:
        return None
    return get_channel_layer()


def broadcast_message(msg, conv) -> None:
    """Push ``msg`` to the conversation's group. Never raises — realtime is a
    convenience over the durable, seq-replayable journal."""
    layer = _channel_layer()
    if layer is None:
        return
    try:
        from asgiref.sync import async_to_sync

        async_to_sync(layer.group_send)(
            group_name(conv.id),
            {"type": "chat.frame", "frame": message_frame(msg, conv)},
        )
    except Exception:  # pragma: no cover - best-effort delivery
        logger.debug("chat realtime fan-out skipped", exc_info=True)
