"""Fan-out: the payloads chat puts on the wire, and where it puts them.

Chat had its own WebSocket implementation — its own frame shapes, its own
close codes, its own resume protocol — written before the fleet had a
substrate. ``stapel-realtime`` is that substrate now, and it says so in its
own module map: *"the fleet had three independent implementations of the same
socket — chat, video and studio-dialog"*. Since 0.3.0 chat is not one of them.
Everything below either builds a payload or hands it to
``stapel_realtime.delivery``; nothing here knows what a Channels group is.

Two streams, and the difference between them is the difference between the
two comm primitives underneath:

``chat:conv:<conversation_id>`` — the **conversation journal**. Resumable:
every frame carries the message's ``rev_seq``, and a client that was away
replays from the durable rows. Delivery is :func:`deliver_frame`, always from
``transaction.on_commit`` — store first, tell the socket second.

``chat:user:<user_id>`` — the participant's **inbox**. Ephemeral: it exists so
a conversation list is live without polling, and everything it carries is
recoverable by re-listing conversations over REST. Delivery is
``stapel_core.comm.signal``.

Payload minimalism, the substrate's review-checklist item, is satisfied on
both: the conversation stream's ``authorize()`` gate *is* participation in
that conversation, and the inbox stream's gate is being that user — so a
message body is admissible on either, because on both the gate equals the
right to read the body. That is why the inbox can carry the last message
instead of an id the client must go and fetch.

Best-effort remains best-effort. No channel layer, no signal transport, redis
down — nothing here raises, because correctness lives in the rows: a client
recovers by replaying ``rev_seq``. What is *not* best-effort any more is the
configuration: a deployment that cannot serve the socket fails
``manage.py check`` (``stapel_chat.E010``-``E014``) rather than quietly
becoming a polling product.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Canonical stream-key module segment.
STREAM_MODULE = "chat"

# ── signal types (never protocol frame types; the core enforces that) ─────

#: A participant's read marker moved.
SIGNAL_READ = "chat.read"
#: A participant's delivery marker moved.
SIGNAL_DELIVERED = "chat.delivered"
#: A participant is typing / recording / uploading. See :mod:`activity`.
SIGNAL_ACTIVITY = "chat.activity"
#: Something happened in a conversation this user takes part in (inbox).
SIGNAL_INBOX = "chat.inbox"
#: A participant connected or went away. See :mod:`stapel_chat.presence`.
#: Ephemeral for the same reason the read receipt is: the durable answer rides
#: back on every participant in the conversation body, so a subscriber that
#: missed the flip learns it on its next read rather than being owed a replay.
SIGNAL_PRESENCE = "chat.presence.changed"


def conversation_stream(conversation_id) -> str:
    """``chat:conv:<id>`` — the resumable journal of one conversation."""
    from stapel_core.comm import stream_key

    return stream_key(STREAM_MODULE, "conv", str(conversation_id))


def user_stream(user_id) -> str:
    """``chat:user:<id>`` — one participant's ephemeral inbox stream."""
    from stapel_core.comm import stream_key

    return stream_key(STREAM_MODULE, "user", str(user_id))


# ── payloads ─────────────────────────────────────────────────────────────


def message_payload(msg, conv=None) -> dict:
    """The one message shape, used by the journal frame, the ``chat.message``
    Action and the REST serializer alike.

    A deleted message keeps its ``id``, ``seq``, ``sender_id`` and
    ``created_at`` and loses everything else: ``body`` is empty,
    ``attachments`` is empty, ``deleted`` is true. That is the tombstone — the
    id keeps arriving so a client cache knows exactly which row to purge.
    """
    conversation_id = conv.id if conv is not None else msg.conversation_id
    deleted = msg.deleted_at is not None
    return {
        "message_id": str(msg.id),
        "conversation_id": str(conversation_id),
        "sender_id": str(msg.sender_id) if msg.sender_id else None,
        "seq": msg.seq,
        "rev_seq": msg.rev_seq,
        "kind": msg.kind,
        "body": "" if deleted else msg.body,
        "reply_to": str(msg.reply_to_id) if msg.reply_to_id else None,
        "attachments": [] if deleted else list(msg.attachments or []),
        "client_msg_id": msg.client_msg_id or None,
        "edited": msg.edited_at is not None,
        "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
        "deleted": deleted,
        "deleted_at": msg.deleted_at.isoformat() if msg.deleted_at else None,
        "created_at": msg.created_at.isoformat(),
    }


# ── delivery ─────────────────────────────────────────────────────────────


def _deliver_frame(stream: str, payload: dict, seq: int) -> bool:
    try:
        from stapel_realtime.delivery import deliver_frame
    except ImportError:  # pragma: no cover - exercised via optional-dep test
        logger.debug("chat: stapel-realtime transport unavailable")
        return False
    return deliver_frame(stream, payload, seq=seq)


def _signal(stream: str, type_: str, payload: dict) -> None:
    try:
        from stapel_core.comm import signal

        signal(stream, type_, payload)
    except Exception:  # pragma: no cover - a courtesy never breaks a caller
        logger.debug("chat: signal %s skipped on %s", type_, stream, exc_info=True)


def broadcast_message(msg, conv, participant_ids=()) -> None:
    """Push a created/edited/deleted message to the conversation journal, then
    nudge every participant's inbox.

    Called from ``transaction.on_commit`` — the row that owns ``rev_seq`` is
    already durable, so a subscriber that misses this replays it.
    """
    payload = message_payload(msg, conv)
    _deliver_frame(conversation_stream(conv.id), payload, seq=msg.rev_seq)
    for user_id in participant_ids:
        _signal(
            user_stream(user_id),
            SIGNAL_INBOX,
            {
                "conversation_id": str(conv.id),
                "conversation_kind": conv.kind,
                "last_seq": conv.last_seq,
                "message": payload,
            },
        )


def broadcast_read(conv, user_id, last_read_seq: int) -> None:
    """A read receipt — ephemeral, because ``last_read_seq`` is on the
    participant row and comes back with the conversation over REST."""
    _signal(
        conversation_stream(conv.id),
        SIGNAL_READ,
        {
            "conversation_id": str(conv.id),
            "user_id": str(user_id),
            "last_read_seq": int(last_read_seq),
        },
    )


def broadcast_delivered(conv, user_id, last_delivered_seq: int) -> None:
    """A delivery receipt. Same reasoning as :func:`broadcast_read`."""
    _signal(
        conversation_stream(conv.id),
        SIGNAL_DELIVERED,
        {
            "conversation_id": str(conv.id),
            "user_id": str(user_id),
            "last_delivered_seq": int(last_delivered_seq),
        },
    )


def broadcast_activity(conversation_id, user_id, state: str, ttl_s: int) -> None:
    """"typing…" and its siblings. Nothing is written; nothing is owed to
    anyone who was not watching. ``ttl_s`` is the client's expiry hint, so no
    "stopped typing" frame is required for the indicator to disappear."""
    _signal(
        conversation_stream(conversation_id),
        SIGNAL_ACTIVITY,
        {
            "conversation_id": str(conversation_id),
            "user_id": str(user_id),
            "state": state,
            "ttl_s": int(ttl_s),
        },
    )


def broadcast_presence(
    conversation_id, user_id, *, online: bool, last_seen_at=None, online_until=None
) -> None:
    """A participant connected or went away.

    Sent on the CONVERSATION stream, not the inbox, because the thread header
    is the surface that renders it and the thread is already subscribed there
    — presence needs no second subscription and no second socket.

    ``last_seen_at`` travels with the flip so an offline header can say *when*
    without a round trip. It is ISO 8601 or null; null means this deployment
    has never seen that user connect.

    ``online_until`` is the lease deadline, and it is what makes an ``online``
    frame **self-limiting**. A flip is announced from a disconnect; a lease
    that simply runs out announces nothing, because nothing happened — no
    socket closed, no row was written, there is no event to send. A client
    told only ``online: true`` therefore believes it forever when the peer's
    socket dies without a disconnect (a killed tab, a lost process), which is
    exactly the case the counter cannot cover and the lease exists for. With
    the deadline on the wire the client reaches the server's own answer on its
    own clock, with no extra traffic, no poll, and no event that would have to
    be invented for a non-happening.
    """
    _signal(
        conversation_stream(conversation_id),
        SIGNAL_PRESENCE,
        {
            "conversation_id": str(conversation_id),
            "user_id": str(user_id),
            "online": bool(online),
            "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
            "online_until": online_until.isoformat() if online_until else None,
        },
    )


def revoke_participant(conversation_id, user_id, reason: str = "left_conversation") -> None:
    """End an open subscription now, when membership ends mid-socket."""
    try:
        from stapel_realtime.delivery import revoke
    except ImportError:  # pragma: no cover
        return
    revoke(conversation_stream(conversation_id), user_id, reason=reason)


__all__ = [
    "SIGNAL_ACTIVITY",
    "SIGNAL_DELIVERED",
    "SIGNAL_INBOX",
    "SIGNAL_READ",
    "STREAM_MODULE",
    "broadcast_activity",
    "broadcast_delivered",
    "broadcast_message",
    "broadcast_read",
    "conversation_stream",
    "message_payload",
    "revoke_participant",
    "user_stream",
]
