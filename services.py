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
from stapel_core.comm import mutate_and_emit

from . import realtime
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
) -> Message:
    """Append a message to ``conversation`` and emit ``chat.message``.

    Allocates the next ``seq`` under a row lock, persists the row and writes the
    outbox event in one transaction; schedules best-effort realtime fan-out
    ``on_commit``. Retries on a seq collision (see ``_MAX_SEQ_RETRIES``).

    ``sender=None`` + ``kind=system`` is the system-line form (assignment,
    resolve, …). ``reply_to`` must belong to the same conversation.
    """
    if reply_to is not None and reply_to.conversation_id != conversation.pk:
        raise InvalidReply("reply_to is not a message of this conversation")
    attachments = list(attachments or [])
    reply_to_id = reply_to.pk if reply_to is not None else None
    last_err: IntegrityError | None = None
    for _ in range(_MAX_SEQ_RETRIES):
        try:
            return _post_once(conversation, sender, body, attachments, reply_to_id, kind)
        except IntegrityError as exc:
            last_err = exc
            continue
    raise last_err  # pragma: no cover - exhausted retries


def _post_once(conversation, sender, body, attachments, reply_to_id, kind) -> Message:
    with mutate_and_emit() as emit:
        # Lock the conversation row so concurrent senders serialize on seq
        # allocation (the unique constraint + retry is the backstop for
        # backends that don't honor the lock).
        conv = Conversation.objects.select_for_update().get(pk=conversation.pk)
        seq = conv.last_seq + 1
        conv.last_seq = seq
        conv.save(update_fields=["last_seq", "updated_at"])
        msg = Message.objects.create(
            conversation=conv,
            sender=sender,
            seq=seq,
            kind=kind,
            body=body,
            reply_to_id=reply_to_id,
            attachments=attachments,
        )
        emit("chat.message", _message_payload(msg, conv), key=str(conv.pk))
        # After commit, push the frame to the realtime group. Best-effort:
        # missed frames are recovered by seq-replay. Not an emit (no outbox),
        # so it must run post-commit against the durable row.
        transaction.on_commit(lambda: realtime.broadcast_message(msg, conv))
    return msg


def _message_payload(msg: Message, conv: Conversation) -> dict:
    return {
        "message_id": str(msg.id),
        "conversation_id": str(conv.id),
        "conversation_kind": conv.kind,
        "scope_key": conv.scope_key,
        "sender_id": str(msg.sender_id) if msg.sender_id else None,
        "seq": msg.seq,
        "kind": msg.kind,
        "body": msg.body,
        "reply_to": str(msg.reply_to_id) if msg.reply_to_id else None,
        "attachments": msg.attachments,
        "created_at": msg.created_at.isoformat(),
    }


# ── Read markers + unread ───────────────────────────────────────────────


def mark_read(*, conversation: Conversation, user, upto_seq: int) -> bool:
    """Advance ``user``'s read marker to ``upto_seq`` (never backwards).
    Returns True if the marker moved."""
    moved = ConversationParticipant.objects.filter(
        conversation=conversation, user=user, last_read_seq__lt=upto_seq
    ).update(last_read_seq=upto_seq)
    return bool(moved)


def unread_count(*, conversation: Conversation, participant: ConversationParticipant) -> int:
    """Messages newer than ``participant``'s read marker, authored by someone
    else. System lines (null sender) are excluded — they never raise a badge."""
    return (
        Message.objects.filter(
            conversation=conversation, seq__gt=participant.last_read_seq
        )
        .filter(sender__isnull=False)
        .exclude(sender_id=participant.user_id)
        .count()
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
