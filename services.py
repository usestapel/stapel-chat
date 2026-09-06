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

import logging
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import (
    BigIntegerField,
    Count,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from stapel_core.comm import mutate_and_emit

from . import realtime
from .activity import resolve_activity
from .attachments import prepare_attachments
# Re-exported deliberately: every caller catches this module's refusals as
# `services.X`, and a view that had to import three modules to handle one send
# would grow a fourth the next time a seam moved.
from .blocks import BlockCheckUnavailable, blocked_pairs  # noqa: F401
from .subjects import UnknownSubjectType, resolve_subject_type  # noqa: F401
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

logger = logging.getLogger(__name__)

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


class ConversationNotFound(LookupError):
    """No such conversation.

    A ``LookupError``, matching :class:`MessageNotFound` and for the same
    reason: a caller on the other end of the bus has to be able to tell "the
    thing you named is gone" (nothing will fix it, stop retrying) from "I
    could not answer" (retry). A comm Function that returned a quiet no-op for
    an unknown id would make a service that lost its thread reference look
    exactly like one whose writes are landing.
    """


class MessageNotFound(LookupError):
    """No such message — or nothing left of it.

    A ``LookupError``, which is the documented contract of the
    ``*.moderation_content`` family: an external moderation module tells
    "the target is gone" (404) from "the owner could not answer" (503) by
    this exception's type, and answering 503 for a message that no longer
    exists would park a case waiting for a row that is never coming back.
    """


class NotEditable(ChatError):
    """A system line, or a message past its EDIT_WINDOW_S."""


class IncompleteSubject(ChatError):
    """Half a subject — a type without a key, or a key without a type."""


class SendRefused(ChatError):
    """This party may not do that here — the module's ONE refusal.

    Raised where a block stands between two people: at send since 0.6.0, and
    since 0.6.1 also when one of them tries to OPEN a new direct thread with
    the other. Deliberately the same exception and the same error key in both
    places. A second refusal vocabulary for the creation door would be a
    second thing to disclose: a client that could tell "refused to open" from
    "refused to send" could tell a block from a coincidence, which is exactly
    what the non-disclosure rule exists to prevent.

    It carries **no reason and no direction**, and it must never grow either:
    the whole point of a block is that the blocked party is not told about it,
    and an exception with a `reason` attribute is an exception whose reason
    ends up in a response body eventually. What the refused party sees is
    indistinguishable from any other closed door.
    """


# ── Conversation creation ───────────────────────────────────────────────


def create_direct(
    *,
    owner,
    other_user_id,
    scope_key: str = "",
    subject_type: str = "",
    subject_key: str = "",
) -> Conversation:
    """Get-or-create the direct thread between ``owner`` and ``other_user_id``
    **about** ``(subject_type, subject_key)``.

    Idempotent by ``(scope_key, {owner, other}, subject)`` — a second call for
    the same pair and the same subject (in either order) returns the existing
    thread rather than a duplicate. The race between two concurrent
    first-creates is resolved by the partial unique constraint on
    ``direct_key``: the loser catches the IntegrityError and returns the
    winner's row.

    **The subject is part of the identity, and that is the change 0.6.0 is
    for.** Through 0.5.x the key was the pair alone, so two people had exactly
    one thread between them forever: a buyer asking about a second listing
    landed in the conversation about the first, under the first one's header.
    Every consumer that cared had to keep its own table mapping many subjects
    onto that one thread and pick which to render. Now the second listing is
    its own thread, and the pair-only thread is simply the one with no
    subject — which every conversation created before this release is.

    An unregistered ``subject_type`` is refused (:class:`UnknownSubjectType`):
    a subject nothing can render is the defect this surface exists to close,
    and storing one would push the failure to the moment somebody opens the
    thread.

    **A block refuses a NEW thread and never an existing one — 0.6.1.** The
    two halves of this verb are not the same act, and the distinction is the
    whole of the change:

    * Opening a thread that does not exist yet is a WRITE, and a blocked pair
      may not perform it. Through 0.6.0 they could: the block only reached the
      send path, so a blocked buyer opened the thread, typed, pressed Enter
      and discovered the wall there. Every consumer that wanted the door shut
      earlier had to keep its own pre-creation check — which is the door
      stapel-classified is deleting now that this exists.
    * Returning a thread that already exists is a READ of history, and **a
      block never deletes history** — the fleet's standing rule. Both parties
      keep seeing everything already said to each other; neither can add to
      it, because the send path (0.6.0) still refuses. Refusing here instead
      would take a conversation away from two people as a side effect of one
      of them tapping "block", and neither of them asked for that.

    So the provider is consulted **only on the create branch**, after the
    lookup has already failed to find a thread. A pair with an existing thread
    costs no block call at all — which also means an outage in the block store
    can never stand between somebody and their own correspondence.
    """
    subject_type = (subject_type or "").strip()
    subject_key = (subject_key or "").strip()
    if subject_type or subject_key:
        # Both or neither. Half a subject renders as badly as a wrong one.
        if not (subject_type and subject_key):
            raise IncompleteSubject(
                "a subject needs both subject_type and subject_key"
            )
        resolve_subject_type(subject_type)

    key = _direct_key(scope_key, owner.pk, other_user_id, subject_type, subject_key)
    existing = Conversation.objects.filter(
        kind=ConversationKind.DIRECT, direct_key=key
    ).first()
    if existing is not None:
        # A read of history. No block call, and therefore no way for the block
        # store's availability to stand between two people and what they
        # already said. See the docstring: this branch is the point of 0.6.1
        # as much as the refusal below is.
        return existing
    _refuse_if_pair_blocked(owner.pk, other_user_id)
    try:
        with mutate_and_emit() as emit:
            conv = Conversation.objects.create(
                kind=ConversationKind.DIRECT,
                scope_key=scope_key,
                direct_key=key,
                subject_type=subject_type,
                subject_key=subject_key,
            )
            ConversationParticipant.objects.bulk_create(
                [
                    ConversationParticipant(conversation=conv, user=owner),
                    ConversationParticipant(conversation=conv, user_id=other_user_id),
                ]
            )
            _emit_created(emit, conv, creator=owner)
        return conv
    except IntegrityError:
        # Lost the create race — return the winner's thread.
        winner = Conversation.objects.filter(
            kind=ConversationKind.DIRECT, direct_key=key
        ).first()
        if winner is not None:
            return winner
        raise


def create_group(
    *,
    owner,
    participant_ids=None,
    scope_key: str = "",
    subject_type: str = "",
    subject_key: str = "",
) -> Conversation:
    """Create a group thread with ``owner`` plus ``participant_ids`` (deduped).
    Group threads are never deduplicated — each call is a new conversation, so
    the subject here is a label rather than half of an identity."""
    subject_type = (subject_type or "").strip()
    subject_key = (subject_key or "").strip()
    if subject_type or subject_key:
        if not (subject_type and subject_key):
            raise IncompleteSubject(
                "a subject needs both subject_type and subject_key"
            )
        resolve_subject_type(subject_type)
    with mutate_and_emit() as emit:
        conv = Conversation.objects.create(
            kind=ConversationKind.GROUP,
            scope_key=scope_key,
            subject_type=subject_type,
            subject_key=subject_key,
        )
        _add_members(conv, owner, participant_ids or [])
        _emit_created(emit, conv, creator=owner)
    return conv


def create_support(*, customer, scope_key: str = "") -> Conversation:
    """Open a support thread for ``customer`` (unassigned, status=open) — it
    lands in the operator queue until an operator is assigned."""
    with mutate_and_emit() as emit:
        conv = Conversation.objects.create(
            kind=ConversationKind.SUPPORT,
            scope_key=scope_key,
            support_status=SupportStatus.OPEN,
        )
        ConversationParticipant.objects.create(conversation=conv, user=customer)
        _emit_created(emit, conv, creator=customer)
    return conv


def _emit_created(emit, conv: Conversation, creator=None) -> None:
    """Write ``chat.conversation.created`` into the outbox, in the same
    transaction as the row.

    Before this existed nothing downstream could react to a new thread: a
    consumer learned a conversation existed only when the first message
    arrived on ``chat.message``, so every binding of a thread to a domain
    object had to be driven from the client that created it — which is a
    client telling the server what happened, in a fleet that is otherwise
    server-authoritative about exactly this.

    Emitted **only on a real create**. ``create_direct`` returning an existing
    thread is not a creation, and a consumer that received one per idempotent
    retry would be right to double-bind.
    """
    emit(
        "chat.conversation.created",
        {
            "conversation_id": str(conv.id),
            "kind": conv.kind,
            "scope_key": conv.scope_key,
            "subject_type": conv.subject_type,
            "subject_key": conv.subject_key,
            "creator_id": str(creator.pk) if creator is not None else None,
            "participant_ids": _participant_ids(conv),
            "created_at": conv.created_at.isoformat(),
        },
        key=str(conv.pk),
    )


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
    _refuse_if_blocked(conversation, sender)
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


def post_system_message(
    conversation_id, body: str, client_msg_id: str = ""
) -> Message:
    """Append one SYSTEM line to a conversation, on behalf of no user.

    The service half of the ``chat.post_system_message`` Function, and
    deliberately not a thin alias for :func:`post_message`: it hard-codes
    ``sender=None`` and ``kind=system``, so neither the bus nor a host
    misconfiguration can turn it into a way of saying something *as a person*.
    In a product where the thread is the record of a deal, a bus-reachable
    "post as this user" is not a convenience, it is a forgery tool.

    Raises :class:`ConversationNotFound` for an unknown or malformed id — the
    caller is holding that id for a reason and must be told it is dead.

    Block rules do not apply: there is no sender for two people to have
    blocked, and a system line is a statement of what happened, not somebody
    reaching somebody. Two people who blocked each other still get the line
    saying their call was declined.
    """
    body = (body or "").strip()
    if not body:
        # The schema already refuses it; this is the second wall, because a
        # blank system line is a message that renders as nothing and cannot
        # be edited or deleted (`edit_message` refuses non-text kinds).
        raise ValueError("a system line needs a body")
    try:
        conv = Conversation.objects.filter(pk=conversation_id).first()
    except (ValidationError, ValueError, TypeError) as exc:
        raise ConversationNotFound(str(conversation_id)) from exc
    if conv is None:
        raise ConversationNotFound(str(conversation_id))
    return post_message(
        conversation=conv,
        sender=None,
        body=body,
        kind=MessageKind.SYSTEM,
        client_msg_id=client_msg_id or "",
    )


def _refuse_if_pair_blocked(user_a, user_b) -> None:
    """Refuse to OPEN a direct thread between two people a block separates.

    The creation half of the same rule ``_refuse_if_blocked`` states for the
    send path, and it obeys the same three: the refusal is
    :class:`SendRefused` — the very same exception and error key a refused
    send raises, so the two doors are not distinguishable from outside — it
    carries no direction, and :class:`BlockCheckUnavailable` travels untouched
    so a failing provider answers 503 rather than opening the thread.

    Only ever called where a thread is about to be **created**. Returning one
    that already exists does not come through here, deliberately.
    """
    pair = (str(user_a), str(user_b))
    if frozenset(pair) in blocked_pairs([pair]):
        raise SendRefused()


def _refuse_if_blocked(conv: Conversation, sender) -> None:
    """Refuse a send that a block stands in the way of.

    Enforced here, in the service, rather than in a view — because the socket
    is the canonical send path since 0.3.0 and a check that lived only in REST
    would be a block that stops nothing anybody actually does.

    **Direct threads only, and never support.** A block is a fact between two
    people; a group room is somebody else's convening and dropping one
    member's messages out of it silently is a different product. A support
    thread is never checked: an operator is not a peer, and a customer who
    blocked an agent would otherwise have muted the help desk.

    Raises :class:`SendRefused` (403, disclosing nothing) when blocked, and
    lets :class:`~stapel_chat.blocks.BlockCheckUnavailable` (503) travel
    untouched when the provider is present and failing. Those two must never
    collapse into one another: a 403 for an outage tells a sender they are
    blocked when they are not, and a delivered message for an outage is the
    provider failing OPEN.
    """
    if sender is None or conv.kind != ConversationKind.DIRECT:
        return
    others = [
        str(uid)
        for uid in ConversationParticipant.objects.filter(conversation=conv)
        .exclude(user_id=sender.pk)
        .values_list("user_id", flat=True)
    ]
    if not others:
        return
    pairs = [(str(sender.pk), other) for other in others]
    hits = blocked_pairs(pairs)
    for a, b in pairs:
        if frozenset((a, b)) in hits:
            raise SendRefused()


def _participant_ids(conv: Conversation) -> list:
    return [
        str(uid)
        for uid in ConversationParticipant.objects.filter(
            conversation=conv
        ).values_list("user_id", flat=True)
    ]


def _fanout_recipients(conv: Conversation) -> list:
    """Whose INBOX stream a frame from this thread goes to.

    Everyone in the thread except those who have left it — a left thread is
    off that person's list, and pushing a row into an inbox that does not
    show it is how a badge appears over nothing. Callers who have just
    written an authored message never lose anybody this way:
    :func:`_resurface_participants` has already cleared the markers by the
    time this reads them.
    """
    return [
        str(uid)
        for uid in ConversationParticipant.objects.filter(
            conversation=conv, left_at__isnull=True
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
        # Before the fan-out reads its recipients: somebody wrote here, so the
        # thread is live again for everyone in it (see `leave_conversation`).
        _resurface_participants(conv, sender)
        emit("chat.message", _message_payload(msg, conv), key=str(conv.pk))
        _schedule_fanout(msg, conv)
    return msg


def _resurface_participants(conv: Conversation, sender) -> None:
    """An authored message un-hides the thread for everyone who had left it.

    The other half of :func:`leave_conversation`, and the reason leaving is
    allowed to be as cheap as it is: nobody can be talked to in a thread they
    cannot see, so a new line brings it back rather than being delivered into
    a room the recipient closed.

    **A SYSTEM line never resurfaces anybody** (``sender is None`` returns
    here), and that is not an optimization: the line that says somebody left
    is itself a system line, and a rule without this clause would put the
    thread straight back in the leaver's inbox with their own goodbye on it.
    """
    if sender is None:
        return
    ConversationParticipant.objects.filter(
        conversation=conv, left_at__isnull=False
    ).update(left_at=None)


def _schedule_fanout(msg: Message, conv: Conversation) -> None:
    """After commit, push the frame to the conversation journal and every
    participant's inbox. Best-effort: a missed frame is recovered by replaying
    ``rev_seq``. Not an emit (no outbox), so it must run post-commit against
    the durable row."""
    recipients = _fanout_recipients(conv)
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


# ── Leaving a conversation ──────────────────────────────────────────────

#: The system marker a departure posts. Machine vocabulary, like every other
#: marker this module writes (``chat.support.resolved``): the words a row
#: draws are the deployment's, through ``STAPEL_CHAT['SYSTEM_LINE_LABELS']``.
#: The body carries the leaver's id after the colon —
#: ``chat.participant.left:<user_id>`` — which is the argument form
#: :func:`system_line_label` already looks a label up around, so a client can
#: render "<name> left the conversation" from the id while the label stays
#: static text this module never interpolates a name into.
SYSTEM_MARKER_LEFT = "chat.participant.left"


def leave_conversation(*, conversation: Conversation, user) -> bool:
    """``user`` leaves ``conversation``. Returns True if anything changed.

    **Leaving hides; it does not remove.** The participant row is stamped
    ``left_at`` and stays: the thread drops off this person's inbox and out of
    their unread counts (:func:`inbox_of`), their open subscription to it is
    revoked, and everyone else keeps the conversation exactly as it was —
    every message, every marker, the counterpart still listed. Nothing is
    deleted for anybody. That is the whole verb, and it is deliberately not
    "delete the conversation": in a marketplace thread the messages are the
    record of a deal between two people, and one of them tidying their inbox
    is not consent from the other to destroy it. **A staff hard delete is not
    here at all** — erasure has one path in this fleet, ``user.deleted`` into
    :class:`~stapel_chat.gdpr.ChatGDPRProvider`, and a second door onto the
    same data is a second door to get wrong.

    **A new message from the other side brings it back.** The marker is
    cleared for everyone in the thread by any *authored* message
    (:func:`_resurface_participants`), which is the honest rule for a 1:1: the
    alternative is a counterpart typing into a room that silently no longer
    reaches anybody, and this module refuses to deliver into a thread nobody
    can see. A system line clears nothing — including the one this function
    posts.

    Idempotent: leaving a thread already left changes nothing, posts no second
    line and returns False.

    A ``system`` line — ``chat.participant.left:<user_id>``, see
    :data:`SYSTEM_MARKER_LEFT` — records the departure in the thread itself,
    so the other participant is told by the transcript rather than by a
    counterpart who quietly stops answering. It raises no unread badge
    (system lines never do) and it resurfaces nobody.
    """
    with transaction.atomic():
        marked = ConversationParticipant.objects.filter(
            conversation=conversation, user=user, left_at__isnull=True
        ).update(left_at=timezone.now())
        if not marked:
            return False
        post_message(
            conversation=conversation,
            sender=None,
            kind=MessageKind.SYSTEM,
            body=f"{SYSTEM_MARKER_LEFT}:{user.pk}",
        )
        # After commit, and only then: the row that authorized the open
        # subscription has to be written before the socket is told about it.
        transaction.on_commit(
            lambda: realtime.revoke_participant(conversation.pk, user.pk)
        )
    return True


def inbox_of(qs, *, viewer):
    """The conversations that belong on ``viewer``'s list.

    A party to the thread who has **not left it** — one rule, in one place,
    because the list, the unread badge and the ``unread=true`` chip all read
    it and a thread that left one of the three would show a badge nobody can
    open. Both conditions land in a single ``filter()`` on purpose: split
    across two calls the ORM joins the participant table twice and matches
    "somebody is you" against "somebody has not left", which is every thread
    with two people in it.
    """
    return qs.filter(
        participants__user=viewer, participants__left_at__isnull=True
    ).distinct()


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


# ── Finding one conversation in an inbox ────────────────────────────────


def _unread_rows(*, viewer):
    """The messages that make a conversation unread FOR ``viewer``.

    ONE definition for the count on every row AND for the ``unread=true``
    filter — a chip that filtered on a different rule than the badge counts on
    is how a list ends up showing rows with no badge on them. It restates
    :func:`unread_count` for a whole page (that one takes a concrete
    participant row; this one an ``OuterRef``), and the two are pinned against
    each other in ``tests/test_inbox_search.py``.

    Anchored on the ``viewer_last_read_seq`` annotation
    (:func:`with_viewer_unread`), so it is a subquery per PAGE, not per row.
    """
    return Message.objects.filter(
        conversation=OuterRef("pk"),
        seq__gt=OuterRef("viewer_last_read_seq"),
        sender__isnull=False,
        deleted_at__isnull=True,
    ).exclude(sender_id=viewer.pk)


def with_viewer_unread(qs, *, viewer):
    """Annotate ``viewer_unread`` on a conversation queryset.

    Two subqueries for a whole page — the read marker, then the count over it —
    where :func:`unread_count` is one query per conversation. The list view
    reads the annotation, and a fifty-row inbox stops costing fifty counts.
    :func:`conversation_to_dto` falls back to the per-row call when it is
    handed a conversation nobody annotated (the single-conversation reads).
    """
    marked = qs.annotate(
        viewer_last_read_seq=Coalesce(
            Subquery(
                ConversationParticipant.objects.filter(
                    conversation=OuterRef("pk"), user=viewer
                ).values("last_read_seq")[:1],
                output_field=BigIntegerField(),
            ),
            Value(0),
            output_field=BigIntegerField(),
        )
    )
    counted = (
        _unread_rows(viewer=viewer)
        .order_by()
        .values("conversation")
        .annotate(n=Count("id"))
        .values("n")
    )
    return marked.annotate(
        viewer_unread=Coalesce(
            Subquery(counted, output_field=IntegerField()),
            Value(0),
            output_field=IntegerField(),
        )
    )


# ── The last line an inbox row draws ─────────────────────────────────────

#: Most characters an inbox preview carries. A row is one line on a phone; the
#: bytes past it are paid for by every row on every page load and rendered by
#: none of them. A client that wants the whole message opens the thread.
PREVIEW_MAX_CHARS = 140


def system_line_label(body: str) -> str | None:
    """The words this deployment gave a system marker — or ``None``.

    A system line's body is machine vocabulary the HOST renders
    (``chat.support.resolved``, ``video.call.ended:188``): this module does not
    own those words in any language, so it ships no labels and the registry
    (``STAPEL_CHAT['SYSTEM_LINE_LABELS']``) is empty out of the box. A marker
    with no label draws nothing on a row and is found by no search — both from
    the one rule in :func:`drawn_last_line`.

    A marker may carry an argument after a colon; the label is looked up on
    the exact body first and then on the part before the colon, and it is
    static text (the argument is never interpolated — a module that formatted
    "188" into a sentence would be inventing the sentence).
    """
    from .conf import chat_settings

    labels = chat_settings.SYSTEM_LINE_LABELS or {}
    if not labels:
        return None
    marker = (body or "").strip()
    label = labels.get(marker)
    if label is None and ":" in marker:
        label = labels.get(marker.split(":", 1)[0])
    return (label or "").strip() or None


def drawn_last_line(*, kind: str, body: str, deleted: bool) -> str | None:
    """THE text an inbox row draws for its last line, or ``None``.

    ONE definition for the preview on every row AND for what
    ``?search=`` matches on the last line — the same shape as
    :func:`_unread_rows` and the unread chip. A row that comes back for a word
    nobody can see on it reads as a bug in the search box; a row that draws a
    word the search will not find reads as the same bug from the other side.

    ``None`` for a tombstone (the row draws "deleted", never the withdrawn
    body), for an attachment-only message (no text at all — the bubble is the
    picture), and for a system marker this deployment gave no words to.
    """
    if deleted:
        return None
    if kind == MessageKind.SYSTEM:
        return system_line_label(body)
    return (body or "").strip() or None


def last_line_reason(
    *, kind: str, body: str, deleted: bool, has_attachments: bool
) -> str | None:
    """Why :func:`drawn_last_line` returned ``None`` for this message.

    ``body_preview: null`` collapses three cases a client cannot tell apart on
    the wire: a tombstone, a message with no body at all, and a system marker
    this deployment gave no words. A row that would have said "Message
    deleted" was saying "Attachment" instead — right for the common case,
    wrong (and worse, never "deleted") for the other. This names which one:

    - ``"deleted"``: a tombstone — the withdrawn body is never drawn.
    - ``"attachment"``: no body, but the message carries attachments — the
      bubble is the picture, not a line of text.
    - ``"system"``: a system marker with no label
      (``STAPEL_CHAT['SYSTEM_LINE_LABELS']``) — machine vocabulary this
      module owns in no language.
    - ``None``: :func:`drawn_last_line` returned words — ``body_preview`` has
      something to show and there is nothing to explain.

    A body-less, attachment-less, non-deleted text message cannot exist over
    the API (``post_message`` refuses it), so that shape falls through to
    ``None`` rather than inventing a fourth reason for it.
    """
    if deleted:
        return "deleted"
    if kind == MessageKind.SYSTEM:
        return None if system_line_label(body) else "system"
    if not (body or "").strip():
        return "attachment" if has_attachments else None
    return None


def preview_of(text: str | None) -> str | None:
    """``text`` as a single plain line of at most :data:`PREVIEW_MAX_CHARS`."""
    if not text:
        return None
    from django.utils.text import Truncator

    flat = " ".join(str(text).split())
    if not flat:
        return None
    return Truncator(flat).chars(PREVIEW_MAX_CHARS)


def _last_line_q(*, needle: str) -> Q:
    """What makes a row's LAST line match ``needle`` — the SQL half of
    :func:`drawn_last_line`, over the very columns the preview is built from
    (the ``last_message_*`` annotations of :func:`with_last_message`).

    Matching the annotations rather than a second subquery of its own is what
    makes "the preview and the search agree" a fact instead of a promise: the
    text this reads IS the text the row ships. The label half is a lookup over
    the settings registry rather than a column (labels are not in this
    database), so it lands as an explicit body list. With no labels configured
    — the default — this is exactly the 0.8.2 rule: a live, authored body,
    matched case-insensitively.
    """
    from .conf import chat_settings

    matches = Q(
        last_message_sender_id__isnull=False,
        last_message_deleted_at__isnull=True,
        last_message_body__icontains=needle,
    )
    lowered = needle.lower()
    labelled = Q()
    for marker, label in (chat_settings.SYSTEM_LINE_LABELS or {}).items():
        if label and lowered in str(label).lower():
            labelled |= Q(last_message_body=marker) | Q(
                last_message_body__startswith=f"{marker}:"
            )
    if labelled:
        matches |= (
            Q(
                last_message_kind=MessageKind.SYSTEM,
                last_message_deleted_at__isnull=True,
            )
            & labelled
        )
    return matches


def _last_message_rows():
    """The one message an inbox row draws: the thread's NEWEST message.

    Newest by ``seq``, and deliberately not ``seq == conversation.last_seq``:
    that counter is shared with the revision journal (:func:`_allocate_seq`
    hands out one sequence for both roles), so the moment anything in a thread
    is edited or deleted, ``last_seq`` is a number no message carries. A row
    matched on it would go blank — and silently un-searchable by its own last
    line — for exactly the threads people have been using most.
    """
    return Message.objects.filter(conversation=OuterRef("pk")).order_by("-seq")


def with_last_message(qs):
    """Annotate the row's last message onto a conversation queryset.

    Seven correlated subqueries in the SELECT the list already runs — the
    same budget as :func:`with_viewer_unread`, which is the whole point: the
    alternative a client is otherwise driven to is ``GET /messages?limit=1``
    per row, and a fifty-row inbox does not get to be fifty requests. Each is
    the first row of the ``(conversation, seq)`` index, read backwards.

    The raw body, the deletion stamp and the attachments list are annotated,
    not the preview or its reason: the text a row DRAWS is decided once, in
    :func:`drawn_last_line`, and why a null one is null is decided once, in
    :func:`last_line_reason` — ``?search=`` reads these same columns
    (:func:`_last_line_q`), so the preview and the search cannot drift apart.
    :func:`~stapel_chat.views.conversation_to_dto` falls back to one query for
    a conversation nobody annotated (the single-conversation reads).

    The sender carries the user model's OWN primary-key field as its output
    field: a bare subquery over an FK column hands back whatever the driver
    stored (SQLite: a UUID with its dashes rubbed out), and an id a client
    cannot match against the participant list on the same row is worse than no
    id at all.
    """
    last = _last_message_rows()
    sender_pk = Message._meta.get_field("sender").target_field
    return qs.annotate(
        last_message_seq=Subquery(last.values("seq")[:1]),
        last_message_kind=Subquery(last.values("kind")[:1]),
        last_message_body=Subquery(last.values("body")[:1]),
        last_message_deleted_at=Subquery(last.values("deleted_at")[:1]),
        last_message_sender_id=Subquery(
            last.values("sender_id")[:1], output_field=sender_pk
        ),
        last_message_created_at=Subquery(last.values("created_at")[:1]),
        last_message_attachments=Subquery(last.values("attachments")[:1]),
    )


def last_message_of(conversation: Conversation):
    """The single message an inbox row would draw, or ``None`` — ONE query.

    The per-row read behind :func:`with_last_message`, for the reads that hold
    one conversation rather than a page (and free for a thread nobody has
    written in: ``last_seq`` is 0, so there is nothing to look up).
    """
    if not conversation.last_seq:
        return None
    return (
        Message.objects.filter(conversation=conversation).order_by("-seq").first()
    )


def _subject_title_matches(qs, *, needle: str):
    """``(ids whose subject card matches, {(type, key): resolution})``.

    A subject's title is not in this database — it belongs to whoever owns the
    subject — so matching it means resolving cards, which is a batched comm
    call per subject type. That is bounded twice: only conversations that carry
    a subject are scanned, and only ``SEARCH_SUBJECT_SCAN`` of them, newest
    first. Threads past the bound are still findable by name and by their last
    line; only the title match stops there, and it says so in the log rather
    than looking like an empty catalogue.

    The resolutions come back so the page that is about to be rendered reuses
    them instead of asking the same provider the same question twice.
    """
    from .conf import chat_settings
    from .subjects import card_matches, get_subject_types, resolve_cards

    limit = int(chat_settings.SEARCH_SUBJECT_SCAN or 0)
    if limit <= 0:
        return [], {}
    rows = list(
        qs.exclude(subject_type="")
        .exclude(subject_key="")
        .order_by("-updated_at")
        # updated_at rides along because this queryset is DISTINCT and Postgres
        # refuses to order a DISTINCT select by a column that is not in it.
        .values_list("id", "subject_type", "subject_key", "updated_at")[: limit + 1]
    )
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
        logger.warning(
            "stapel_chat: inbox search scanned only the newest %d subject "
            "threads; older ones were matched by name and last line only "
            "(raise STAPEL_CHAT['SEARCH_SUBJECT_SCAN'] if that is too few)",
            limit,
        )
    resolved = resolve_cards({(row[1], row[2]) for row in rows})
    types = get_subject_types()
    ids = [
        row[0]
        for row in rows
        if card_matches(
            row[1], (resolved.get((row[1], row[2])) or {}).get("card"), needle, types
        )
    ]
    return ids, resolved


def _search_inbox(qs, *, viewer, needle: str):
    """Narrow a conversation queryset to the rows a person could recognise by
    ``needle`` — ``(queryset, {(type, key): resolution})``.

    Three fields, and they are exactly the three an inbox row DRAWS: WHO the
    thread is with (the counterpart's display name, over the user-model fields
    ``SEARCH_NAME_FIELDS`` names), WHAT it is about (the subject card's title),
    and the last line. A row cannot be found by a word that is nowhere on it,
    which is why ids, kinds and timestamps are not searched, and why the last
    line is matched only when the row would actually show it: a tombstone
    renders as "deleted" and a system line as machine vocabulary
    (``video.call.ended:188``), so neither is searchable text. "Would actually
    show it" is not a second opinion here: it is :func:`drawn_last_line`, the
    rule that also produces the row's ``last_message.body_preview``. A marker
    this deployment DID give words to (``SYSTEM_LINE_LABELS``) is drawn and
    found by those words; one it did not is neither drawn nor found.

    The match is a case-insensitive substring (``icontains``), and it FILTERS —
    the anchor pagination then pages the filtered set, so ``anchor`` /
    ``direction`` / ``limit`` mean exactly what they mean without a search.
    """
    needle = (needle or "").strip()
    if not needle:
        return qs, {}

    from .conf import chat_settings

    name_q = Q()
    for path in chat_settings.SEARCH_NAME_FIELDS or ():
        name_q |= Q(**{f"user__{str(path).strip()}__icontains": needle})

    matched = Q()
    if name_q:
        matched |= Q(
            Exists(
                ConversationParticipant.objects.filter(conversation=OuterRef("pk"))
                .exclude(user_id=viewer.pk)
                .filter(name_q)
            )
        )
    # The LAST line — the newest message in the thread, not any older one that
    # happens to contain the word, and read off the very annotations the row's
    # `last_message.body_preview` is rendered from. `drawn_last_line` is the
    # rule; `_last_line_q` is its SQL half.
    if "last_message_seq" not in qs.query.annotations:
        qs = with_last_message(qs)
    matched |= _last_line_q(needle=needle)
    subject_ids, resolutions = _subject_title_matches(qs, needle=needle)
    if subject_ids:
        matched |= Q(id__in=subject_ids)
    return qs.filter(matched), resolutions


def filter_inbox(qs, *, viewer, search: str = "", unread_only: bool = False):
    """The conversation list's two filters, applied BEFORE pagination.

    Returns ``(queryset, {(subject_type, subject_key): resolution})`` — the
    second is whatever the search already resolved, handed to
    :func:`subject_cards_for` so one request asks a card provider once.
    """
    if unread_only:
        if "viewer_unread" not in qs.query.annotations:
            qs = with_viewer_unread(qs, viewer=viewer)
        qs = qs.filter(viewer_unread__gt=0)
    return _search_inbox(qs, viewer=viewer, needle=search)


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


# ── Moderation seam (read half) ─────────────────────────────────────────


#: How stapel-classified names a chat message to stapel-moderation:
#: ``<conversation_id>:<message_id>``. The conversation half is not
#: decoration — it is what lets THAT package answer, off its own join table,
#: whether the reporter was in the thread; nobody can answer it from a bare
#: message id. This module accepts either spelling and, given the composite
#: one, checks that the halves agree.
MESSAGE_KEY_SEPARATOR = ":"


def _split_message_key(target_key) -> tuple:
    """``"<conversation_id>:<message_id>"`` -> (conversation_id, message_id).

    A bare id comes back as ``("", id)`` — both spellings are served, because
    the composite key is a composite's convention and a deployment without
    that composite has no conversation half to send.
    """
    text = str(target_key or "")
    if MESSAGE_KEY_SEPARATOR not in text:
        return "", text
    conversation_id, _, message_id = text.rpartition(MESSAGE_KEY_SEPARATOR)
    return conversation_id, message_id


def moderation_content(message_id) -> dict:
    """Return ``message_id``'s content for an external moderation module.

    The read half of the moderation seam, the ``*.moderation_content`` family
    (`listings.moderation_content`, `reviews.moderation_content`): identifiers
    travel on the bus, the content is fetched at the moment it is looked at,
    so a moderator opening a case hours later reads the message as it is now
    — including an edit made after the complaint was filed.

    **A tombstone is "gone", not "empty".** A deleted or erased message has
    an empty body by construction, and handing that back would show a
    moderator a blank card indistinguishable from a message that said
    nothing. It raises :class:`MessageNotFound` instead, which the moderation
    module renders as ``target_not_found`` — nothing is down, there is simply
    nothing left to look at. That is also the right answer after a GDPR
    erasure: the content is destroyed, and a moderation case must not become
    the one place it survives.

    ``message_id`` is either a bare message id or the composite key a
    composite uses to name a message (:data:`MESSAGE_KEY_SEPARATOR`); given
    the composite form, both halves must agree or the message is not this
    target.

    Attachment KEYS ride in ``media``; the module stores no bytes and the
    keys are opaque, so a console resolves them through the host's CDN. The
    ``chat_message`` policy therefore ships ``media: False`` — feeding opaque
    keys to a vision screener would only buy a refusal.

    Unguarded on purpose: this is a comm Function, and who may look at a
    case's content is the moderation module's ``can_view_content`` policy to
    answer, not a second gate here that would fail closed against it.
    """
    conversation_id, key = _split_message_key(message_id)
    try:
        message = (
            Message.objects.select_related("conversation").filter(pk=key).first()
        )
    except (ValueError, ValidationError) as exc:
        raise MessageNotFound(str(message_id)) from exc
    if message is None or message.deleted_at is not None:
        raise MessageNotFound(str(message_id))
    conv = message.conversation
    if conversation_id and str(conv.id) != conversation_id:
        # The two halves of a composite key must agree. A message quoted
        # under somebody else's conversation is not this target.
        raise MessageNotFound(str(message_id))
    return {
        "text": message.body,
        # A conversation has no title and a message has no declared language
        # — empty rather than invented (the family's convention).
        "title": "",
        "language": "",
        "media": [str(a.get("key") or "") for a in (message.attachments or []) if a],
        # Null for a system line, which is exactly what makes "you cannot
        # report your own content" and "sanction the right person"
        # answerable — and unanswerable for a line nobody wrote.
        "author_id": str(message.sender_id) if message.sender_id else "",
        "url": "",
        "kind": message.kind,
        "conversation_id": str(conv.id),
        "conversation_kind": conv.kind,
        "scope_key": conv.scope_key,
        "seq": message.seq,
        "edited": message.edited_at is not None,
        "created_at": message.created_at.isoformat(),
    }


# ── Reads other modules need (comm) ─────────────────────────────────────


def conversation_participants(conversation_ids) -> dict:
    """``[conversation_id, …] -> {id: {exists, kind, subject, participants}}``.

    The read stapel-classified stored ``initiator_id`` / ``counterparty_id``
    on its own row to avoid making, because chat exposed no way to ask *"is
    this user a party to this conversation"* — so a module that already knew
    the conversation id still had to keep its own copy of who was in it, and
    that copy could be wrong the moment anything changed here.

    **Batch, and it answers for every id it was asked about.** An id that names
    no conversation comes back ``{"exists": false}`` rather than being dropped,
    for the same reason ``classified.subject_cards`` answers a deleted listing
    with a ``gone`` card: the caller is holding that id for a reason and needs
    to be told it is dead, not left to infer it from an absence.

    Deliberately NOT a permission check. It answers *who is a party*; whether
    that means the caller may do a thing is the caller's rule, and a helper
    named ``can_x`` here would be this module deciding another module's policy.
    """
    wanted = [str(cid) for cid in (conversation_ids or []) if str(cid or "").strip()]
    if not wanted:
        return {}

    valid, out = [], {}
    for cid in wanted:
        try:
            uuid.UUID(cid)
        except (ValueError, AttributeError, TypeError):
            # A malformed id is "no such conversation", not a 500 — the same
            # rule moderation_content applies to a key that is not an id.
            out[cid] = {"exists": False, "kind": "", "subject_type": "",
                        "subject_key": "", "scope_key": "", "participants": []}
            continue
        valid.append(cid)

    rows = {
        str(c.id): c
        for c in Conversation.objects.filter(id__in=valid).prefetch_related(
            "participants"
        )
    }
    for cid in valid:
        conv = rows.get(cid)
        if conv is None:
            out[cid] = {"exists": False, "kind": "", "subject_type": "",
                        "subject_key": "", "scope_key": "", "participants": []}
            continue
        out[cid] = {
            "exists": True,
            "kind": conv.kind,
            "scope_key": conv.scope_key,
            "subject_type": conv.subject_type,
            "subject_key": conv.subject_key,
            "participants": [
                {"user_id": str(p.user_id), "role": p.role}
                for p in conv.participants.all()
            ],
        }
    return out


def presence_for(conversations, viewer=None) -> dict:
    """``[Conversation, …] -> {user_id: {"online", "last_seen_at"}}``.

    One query for every participant of a whole page, the same shape of read as
    :func:`subject_cards_for` and for the same reason: a header that has to
    ask per conversation is a header that will be asked fifty times.

    ``viewer`` is who the answer is FOR. A guest — signed in far enough to
    pass ``IsAuthenticated``, with nobody having registered — is answered with
    an empty map, and the DTO then ships the offline default that every caller
    already renders (:mod:`stapel_chat.presence`). Omitting the argument keeps
    the unfiltered read for server-side callers; every HTTP path passes one.
    """
    from .presence import readable_by, snapshot

    if viewer is not None and not readable_by(viewer):
        return {}

    user_ids = {
        str(p.user_id) for c in conversations for p in c.participants.all()
    }
    return snapshot(user_ids)


def subject_cards_for(conversations, resolved=None) -> dict:
    """``[Conversation, …] -> {conversation_id: resolution}`` — one call per
    subject type for the whole list, never one per conversation.

    A conversation with no subject is simply absent from the answer; that is
    not a degradation, it is a thread about nothing in particular.

    ``resolved`` is a ``{(type, key): resolution}`` map a caller already has —
    the inbox search resolves cards to match titles, and asking the same
    provider for the same keys again in the same request would be a second
    round trip for an answer already in hand. Only the pairs it does not cover
    are fetched.
    """
    from .subjects import resolve_cards

    pairs = {
        (c.subject_type, c.subject_key)
        for c in conversations
        if c.subject_type and c.subject_key
    }
    if not pairs:
        return {}
    known = {pair: resolution for pair, resolution in (resolved or {}).items()}
    missing = pairs - set(known)
    if missing:
        known.update(resolve_cards(missing))
    return {
        str(c.id): known[(c.subject_type, c.subject_key)]
        for c in conversations
        if (c.subject_type, c.subject_key) in known
    }
