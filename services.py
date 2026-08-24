"""Domain services for stapel-chat.

The generic messaging core: conversation creation (direct is idempotent by
participant pair), the transactional send path (allocate a monotonic ``seq``,
persist the row and emit ``chat.message`` in one transaction), read markers and
unread counts, and the support lifecycle (queue → assign → resolve → reopen)
built on the same model.

Ordering + delivery contract: every message carries a per-conversation ``seq``
allocated under a row lock from ``Conversation.last_seq`` (gapless, monotonic).
The ``chat.message`` emit is written into the outbox inside the same
transaction as the row (mutate_and_emit), so a subscriber never sees an event
for a message that did not commit. Realtime fan-out to the Channels group is
scheduled ``on_commit`` and is best-effort — clients that miss it replay from
the durable rows by ``seq``.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone
from stapel_core.comm import mutate_and_emit

from . import realtime
from .activity import resolve_activity
from .attachments import prepare_attachments
from .models import (
    Conversation,
    ConversationKind,
    ConversationParticipant,
    Message,
    MessageKind,
    ParticipantRole,
    SupportStatus,
    _direct_key,
)

#: Cap on seq-allocation retries. select_for_update serializes senders on a real
#: DB (one retry at most); the retry loop is the backstop for backends without
#: row locking, where the (conversation, seq) unique constraint catches a race.
_MAX_SEQ_RETRIES = 8


class ChatError(Exception):
    """Base for service-layer refusals mapped to error responses by views."""


class AlreadyAssigned(ChatError):
    """A support conversation is already assigned to a different operator."""


class NotSupport(ChatError):
    """A support-only operation was attempted on a direct/group conversation."""


class InvalidReply(ChatError):
    """reply_to points at a message that is not in this conversation."""


class NotAuthor(ChatError):
    """Only a message's own author may edit or delete it."""


class MessageGone(ChatError):
    """The message is already a tombstone — there is nothing left to change."""


class NotEditable(ChatError):
    """A system line, or a message past its EDIT_WINDOW_S."""


# ── Conversation creation ───────────────────────────────────────────────


def create_direct(*, owner, other_user_id, scope_key: str = "") -> Conversation:
    """Get-or-create the direct thread between ``owner`` and ``other_user_id``.

    Idempotent by ``(scope_key, {owner, other})`` — a second call for the same
    pair (in either order) returns the existing thread rather than a duplicate.
    The race between two concurrent first-creates is resolved by the partial
    unique constraint on ``direct_key``: the loser catches the IntegrityError
    and returns the winner's row.
    """
    key = _direct_key(scope_key, owner.pk, other_user_id)
    existing = Conversation.objects.filter(
        kind=ConversationKind.DIRECT, direct_key=key
    ).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            conv = Conversation.objects.create(
                kind=ConversationKind.DIRECT, scope_key=scope_key, direct_key=key
            )
            ConversationParticipant.objects.bulk_create(
                [
                    ConversationParticipant(conversation=conv, user=owner),
                    ConversationParticipant(conversation=conv, user_id=other_user_id),
                ]
            )
        return conv
    except IntegrityError:
        # Lost the create race — return the winner's thread.
        winner = Conversation.objects.filter(
            kind=ConversationKind.DIRECT, direct_key=key
        ).first()
        if winner is not None:
            return winner
        raise


def create_group(*, owner, participant_ids=None, scope_key: str = "") -> Conversation:
    """Create a group thread with ``owner`` plus ``participant_ids`` (deduped).
    Group threads are never deduplicated — each call is a new conversation."""
    conv = Conversation.objects.create(
        kind=ConversationKind.GROUP, scope_key=scope_key
    )
    _add_members(conv, owner, participant_ids or [])
    return conv


def create_support(*, customer, scope_key: str = "") -> Conversation:
    """Open a support thread for ``customer`` (unassigned, status=open) — it
    lands in the operator queue until an operator is assigned."""
    conv = Conversation.objects.create(
        kind=ConversationKind.SUPPORT,
        scope_key=scope_key,
        support_status=SupportStatus.OPEN,
    )
    ConversationParticipant.objects.create(conversation=conv, user=customer)
    return conv


def _add_members(conv: Conversation, owner, participant_ids) -> None:
    rows = [ConversationParticipant(conversation=conv, user=owner)]
    seen = {str(owner.pk)}
    for uid in participant_ids:
        if str(uid) in seen:
            continue
        seen.add(str(uid))
        rows.append(ConversationParticipant(conversation=conv, user_id=uid))
    ConversationParticipant.objects.bulk_create(rows, ignore_conflicts=True)


# ── Sending ─────────────────────────────────────────────────────────────


def post_message(
    *,
    conversation: Conversation,
    sender=None,
    body: str = "",
    attachments=None,
    reply_to=None,
    kind: str = MessageKind.TEXT,
    client_msg_id: str = "",
) -> Message:
    """Append a message to ``conversation`` and emit ``chat.message``.

    Allocates the next ``seq`` under a row lock, persists the row and writes the
    outbox event in one transaction; schedules best-effort realtime fan-out
    ``on_commit``. Retries on a seq collision (see ``_MAX_SEQ_RETRIES``).

    ``sender=None`` + ``kind=system`` is the system-line form (assignment,
    resolve, …). ``reply_to`` must belong to the same conversation.

    ``client_msg_id`` makes the send **idempotent**: a client that pressed
    Enter, lost the socket before the frame came back and retried gets the
    same message, not a second one. Passing the same id twice returns the
    first row untouched (no new seq, no second emit).

    Attachments arrive as descriptors and are normalized + enriched from the
    CDN here, once, so every later reader — REST, replay, the Action — sees
    the same metadata and no consumer has to resolve a ref again.
    """
    if reply_to is not None and reply_to.conversation_id != conversation.pk:
        raise InvalidReply("reply_to is not a message of this conversation")
    client_msg_id = (client_msg_id or "").strip()[:64]
    if client_msg_id:
        existing = Message.objects.filter(
            conversation_id=conversation.pk, client_msg_id=client_msg_id
        ).first()
        if existing is not None:
            return existing
    attachments = prepare_attachments(attachments)
    reply_to_id = reply_to.pk if reply_to is not None else None
    last_err: IntegrityError | None = None
    for _ in range(_MAX_SEQ_RETRIES):
        try:
            return _post_once(
                conversation, sender, body, attachments, reply_to_id, kind,
                client_msg_id,
            )
        except IntegrityError as exc:
            last_err = exc
            if client_msg_id:
                # The collision may be the idempotency constraint rather than
                # the seq one — a concurrent retry of the same send.
                twin = Message.objects.filter(
                    conversation_id=conversation.pk, client_msg_id=client_msg_id
                ).first()
                if twin is not None:
                    return twin
            continue
    raise last_err  # pragma: no cover - exhausted retries


def _participant_ids(conv: Conversation) -> list:
    return [
        str(uid)
        for uid in ConversationParticipant.objects.filter(
            conversation=conv
        ).values_list("user_id", flat=True)
    ]


def _allocate_seq(conversation_pk) -> tuple[Conversation, int]:
    """Lock the conversation row and take the next journal sequence.

    One counter serves both roles: a message's position in the thread when it
    is created, and its position in the revision journal every time it
    changes. That is what keeps ``rev_seq`` monotonic across creates, edits
    and deletes without a second counter to keep in step.
    """
    conv = Conversation.objects.select_for_update().get(pk=conversation_pk)
    seq = conv.last_seq + 1
    conv.last_seq = seq
    conv.save(update_fields=["last_seq", "updated_at"])
    return conv, seq


def _post_once(
    conversation, sender, body, attachments, reply_to_id, kind, client_msg_id
) -> Message:
    with mutate_and_emit() as emit:
        # Lock the conversation row so concurrent senders serialize on seq
        # allocation (the unique constraint + retry is the backstop for
        # backends that don't honor the lock).
        conv, seq = _allocate_seq(conversation.pk)
        msg = Message.objects.create(
            conversation=conv,
            sender=sender,
            seq=seq,
            rev_seq=seq,
            kind=kind,
            body=body,
            reply_to_id=reply_to_id,
            attachments=attachments,
            client_msg_id=client_msg_id,
        )
        emit("chat.message", _message_payload(msg, conv), key=str(conv.pk))
        _schedule_fanout(msg, conv)
    return msg


def _schedule_fanout(msg: Message, conv: Conversation) -> None:
    """After commit, push the frame to the conversation journal and every
    participant's inbox. Best-effort: a missed frame is recovered by replaying
    ``rev_seq``. Not an emit (no outbox), so it must run post-commit against
    the durable row."""
    recipients = _participant_ids(conv)
    transaction.on_commit(
        lambda: realtime.broadcast_message(msg, conv, participant_ids=recipients)
    )


def _message_payload(msg: Message, conv: Conversation) -> dict:
    """The ``chat.message`` Action payload: the wire shape plus the two fields
    only a server-side subscriber needs (thread kind, scope)."""
    payload = realtime.message_payload(msg, conv)
    payload["conversation_kind"] = conv.kind
    payload["scope_key"] = conv.scope_key
    return payload


# ── Editing and deletion ────────────────────────────────────────────────


def edit_message(*, message: Message, editor, body: str) -> Message:
    """Replace a message's body, stamp ``edited_at``, re-journal it.

    The edit takes a **fresh** ``rev_seq``. That is the whole mechanism: a
    client that was offline when the edit happened has already acknowledged
    the message's original ``seq``, so nothing anchored on ``seq`` would ever
    reach it again. Anchored on ``rev_seq``, the edited row is simply part of
    the next replay.
    """
    from .conf import chat_settings

    if message.deleted_at is not None:
        raise MessageGone("this message has been deleted")
    if message.kind != MessageKind.TEXT:
        raise NotEditable("system lines are not editable")
    if message.sender_id is None or str(message.sender_id) != str(editor.pk):
        raise NotAuthor("only the author may edit a message")
    window = int(chat_settings.EDIT_WINDOW_S or 0)
    if window > 0:
        age = (timezone.now() - message.created_at).total_seconds()
        if age > window:
            raise NotEditable("the edit window for this message has closed")

    with mutate_and_emit() as emit:
        conv, rev = _allocate_seq(message.conversation_id)
        message.body = body
        message.edited_at = timezone.now()
        message.rev_seq = rev
        message.save(update_fields=["body", "edited_at", "rev_seq"])
        emit("chat.message.edited", _message_payload(message, conv), key=str(conv.pk))
        _schedule_fanout(message, conv)
    return message


def delete_message(*, message: Message, actor, hard: bool = False) -> Message:
    """Turn a message into a **tombstone** and re-journal it.

    Not a removal. The row keeps its ``id``, its ``seq`` and its place in the
    order; ``body`` and ``attachments`` are emptied and ``deleted_at`` is
    stamped. It then goes out again under a fresh ``rev_seq``, which is what
    lets every client cache, service worker and offline database learn *which
    id to purge*. A row that vanished from the table would leave those copies
    in place forever — a delete that deletes nothing on the only machines that
    still show the message.

    Deleting an already-deleted message is idempotent (no new sequence, no
    second event): a retried delete must not consume journal positions.

    ``hard=True`` is reserved for the erasure path (GDPR), which owns the one
    reason a row may actually leave the table.
    """
    if message.sender_id is None or str(message.sender_id) != str(actor.pk):
        raise NotAuthor("only the author may delete a message")
    if message.deleted_at is not None:
        return message

    with mutate_and_emit() as emit:
        conv, rev = _allocate_seq(message.conversation_id)
        message.body = ""
        message.attachments = []
        message.deleted_at = timezone.now()
        message.rev_seq = rev
        message.save(
            update_fields=["body", "attachments", "deleted_at", "rev_seq"]
        )
        emit(
            "chat.message.deleted",
            {
                "message_id": str(message.id),
                "conversation_id": str(conv.id),
                "conversation_kind": conv.kind,
                "scope_key": conv.scope_key,
                "sender_id": str(message.sender_id) if message.sender_id else None,
                "seq": message.seq,
                "rev_seq": message.rev_seq,
                "deleted_at": message.deleted_at.isoformat(),
            },
            key=str(conv.pk),
        )
        _schedule_fanout(message, conv)
    return message


# ── Erasure (the one path that may unmake authored content) ─────────────


def erase_user_messages(user_id) -> int:
    """Turn every message a user wrote into an **anonymous tombstone**.

    The erasure path, called from the GDPR provider. It used to be
    ``Message.objects.filter(sender_id=...).delete()``, and that was wrong in
    two ways at once. It tore holes in a sequence the whole protocol assumes
    is gapless, and — the part that matters for the person exercising the
    right — it removed the rows from the server while leaving every copy on
    every other participant's device, because nothing told those devices which
    ids had ceased to exist.

    A tombstone erases *and* propagates: body gone, attachments gone, sender
    detached, and a fresh ``rev_seq`` so the next replay hands every client the
    ids to purge. Content is destroyed either way; only this way does the
    destruction travel.

    Sequences are reserved as one contiguous block per conversation — one lock
    per thread rather than one per message, and distinct positions so the
    socket's per-seq deduplication does not swallow all but the first frame.
    """
    touched = 0
    conv_ids = list(
        Message.objects.filter(sender_id=user_id, deleted_at__isnull=True)
        .values_list("conversation_id", flat=True)
        .distinct()
    )
    now = timezone.now()
    for conv_id in conv_ids:
        with transaction.atomic():
            conv = Conversation.objects.select_for_update().get(pk=conv_id)
            rows = list(
                Message.objects.filter(
                    conversation_id=conv_id, sender_id=user_id, deleted_at__isnull=True
                ).order_by("seq")
            )
            if not rows:
                continue
            base = conv.last_seq
            conv.last_seq = base + len(rows)
            conv.save(update_fields=["last_seq", "updated_at"])
            for offset, msg in enumerate(rows, start=1):
                Message.objects.filter(pk=msg.pk).update(
                    body="",
                    attachments=[],
                    sender=None,
                    deleted_at=now,
                    rev_seq=base + offset,
                )
                msg.body = ""
                msg.attachments = []
                msg.sender_id = None
                msg.deleted_at = now
                msg.rev_seq = base + offset
            recipients = _participant_ids(conv)
            for msg in rows:
                transaction.on_commit(
                    (lambda m, c, r: lambda: realtime.broadcast_message(
                        m, c, participant_ids=r
                    ))(msg, conv, recipients)
                )
            touched += len(rows)
    # Membership and empty-thread cleanup belong to the caller (the GDPR
    # provider), which knows the order those steps have to happen in.
    return touched


# ── Activity states (ephemeral, nothing is written) ─────────────────────


def announce_activity(*, conversation: Conversation, user, state: str) -> dict:
    """Fan out "typing…" (or any registered state) to the conversation.

    Nothing is persisted and nothing is owed to a participant who is not
    connected — this is the Signal primitive in its purest form. The returned
    dict is the resolved ``{state, ttl_s}``; an unregistered state raises
    :class:`~stapel_chat.activity.UnknownActivityState`.
    """
    resolved = resolve_activity(state)
    realtime.broadcast_activity(
        conversation.pk, user.pk, resolved["state"], resolved["ttl_s"]
    )
    return resolved


# ── Read markers + unread ───────────────────────────────────────────────


def mark_read(*, conversation: Conversation, user, upto_seq: int) -> bool:
    """Advance ``user``'s read marker to ``upto_seq`` (never backwards).

    Returns True if the marker moved — and, when it did, fans out a
    ``chat.read`` receipt. The receipt is a Signal, not an event: the durable
    truth is the participant row, which every conversation response already
    carries, so a subscriber who was away reads the marker instead of being
    owed a replay of it.
    """
    moved = ConversationParticipant.objects.filter(
        conversation=conversation, user=user, last_read_seq__lt=upto_seq
    ).update(last_read_seq=upto_seq)
    if moved:
        realtime.broadcast_read(conversation, user.pk, upto_seq)
    return bool(moved)


def mark_delivered(*, conversation: Conversation, user, upto_seq: int) -> bool:
    """Advance ``user``'s **delivery** marker (never backwards).

    Delivered is a weaker fact than read and a real one: the recipient's
    client holds the message, nobody has looked at it. Keeping the two apart
    is what lets a UI draw one tick and two, and it is the client — not the
    server — that knows the difference, which is why this is an explicit call
    rather than something inferred from a socket being open.
    """
    moved = ConversationParticipant.objects.filter(
        conversation=conversation, user=user, last_delivered_seq__lt=upto_seq
    ).update(last_delivered_seq=upto_seq)
    if moved:
        realtime.broadcast_delivered(conversation, user.pk, upto_seq)
    return bool(moved)


def unread_count(*, conversation: Conversation, participant: ConversationParticipant) -> int:
    """Messages newer than ``participant``'s read marker, authored by someone
    else. System lines (null sender) are excluded — they never raise a badge —
    and so are tombstones: a message that was deleted before you got to it
    must not leave a badge you can never clear by reading anything."""
    return (
        Message.objects.filter(
            conversation=conversation, seq__gt=participant.last_read_seq
        )
        .filter(sender__isnull=False, deleted_at__isnull=True)
        .exclude(sender_id=participant.user_id)
        .count()
    )


def journal_rows(*, conversation_id, after_seq: int, limit: int):
    """Rows a resuming socket has not seen, ordered by ``rev_seq``.

    The replay source for :class:`~stapel_chat.consumers.ChatConsumer`.
    Anchored on ``rev_seq``, **not** ``seq``, so an old message edited or
    deleted while the client was away is part of the catch-up. Tombstones are
    included on purpose — that is the whole point of keeping them.
    """
    return (
        Message.objects.filter(
            conversation_id=conversation_id, rev_seq__gt=after_seq
        )
        .select_related("conversation")
        .order_by("rev_seq")[:limit]
    )


# ── Support lifecycle ───────────────────────────────────────────────────


def support_queue(qs=None):
    """Unassigned, still-active support conversations (the operator queue),
    oldest first. ``qs`` lets a caller pre-scope (e.g. by SCOPE_PROVIDER)."""
    base = qs if qs is not None else Conversation.objects.all()
    return base.filter(
        kind=ConversationKind.SUPPORT,
        assigned_operator__isnull=True,
        support_status__in=[SupportStatus.OPEN, SupportStatus.PENDING],
    ).order_by("created_at")


def assign_operator(*, conversation: Conversation, operator) -> Conversation:
    """Assign ``operator`` to a support conversation (first-come).

    Idempotent for the same operator; a different operator on an
    already-assigned thread raises :class:`AlreadyAssigned`. Adds the operator
    as an ``operator`` participant, emits ``chat.support.assigned`` and posts a
    system line.
    """
    if conversation.kind != ConversationKind.SUPPORT:
        raise NotSupport("assign applies only to support conversations")
    with mutate_and_emit() as emit:
        conv = Conversation.objects.select_for_update().get(pk=conversation.pk)
        if conv.assigned_operator_id and conv.assigned_operator_id != operator.id:
            raise AlreadyAssigned("support conversation already assigned")
        already = conv.assigned_operator_id == operator.id
        conv.assigned_operator = operator
        conv.save(update_fields=["assigned_operator", "updated_at"])
        ConversationParticipant.objects.update_or_create(
            conversation=conv,
            user=operator,
            defaults={"role": ParticipantRole.OPERATOR},
        )
        if not already:
            emit(
                "chat.support.assigned",
                {
                    "conversation_id": str(conv.id),
                    "operator_id": str(operator.id),
                    "scope_key": conv.scope_key,
                },
                key=str(conv.id),
            )
    if not already:
        post_message(
            conversation=conv, sender=None, kind=MessageKind.SYSTEM,
            body="chat.support.assigned",
        )
    return conv


def set_support_status(
    *, conversation: Conversation, status: str, system_marker: str | None = None
) -> Conversation:
    """Set a support conversation's status (open/pending/resolved) and,
    optionally, post a system line marking the transition."""
    if conversation.kind != ConversationKind.SUPPORT:
        raise NotSupport("status applies only to support conversations")
    conversation.support_status = status
    conversation.save(update_fields=["support_status", "updated_at"])
    if system_marker:
        post_message(
            conversation=conversation, sender=None, kind=MessageKind.SYSTEM,
            body=system_marker,
        )
    return conversation


def resolve_support(*, conversation: Conversation) -> Conversation:
    """Mark a support conversation resolved."""
    return set_support_status(
        conversation=conversation,
        status=SupportStatus.RESOLVED,
        system_marker="chat.support.resolved",
    )


def reopen_support(*, conversation: Conversation) -> Conversation:
    """Reopen a resolved support conversation back into the OPEN state."""
    return set_support_status(
        conversation=conversation,
        status=SupportStatus.OPEN,
        system_marker="chat.support.reopened",
    )
