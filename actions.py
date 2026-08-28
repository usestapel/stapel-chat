"""Action subscriptions of stapel-chat.

Handlers must be idempotent: delivery is at-least-once (outbox retries, broker
redelivery). Consumes contracts live in ``schemas/consumes/``.
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


class MergeTargetNotReady(RuntimeError):
    """A ``user.merged`` arrived before the surviving account exists here.

    Transient, not a bug: the guest has chat to carry over but there is no
    local user row to point their FKs at yet. Raising is the comm layer's
    retry signal — ``deliver()`` wraps a failing handler in
    ``ActionDeliveryError`` and the outbox redelivers — so the transfer
    completes once the survivor's user projection lands. An operator seeing
    this in a redelivery loop is looking at an ordering lag, not a defect.
    """


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase this module's PII when an account deletion is executed: the user's
    authored messages, their conversation participations, and any direct
    conversation that becomes empty as a result."""
    from .gdpr import ChatGDPRProvider

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    ChatGDPRProvider().delete(user_id)
    logger.info("chat data erased for deleted user %s", user_id)


@on_action("user.merged")
def handle_user_merged(event):
    """Carry a merged-away account's chat over to the account that survives.

    stapel-auth deletes the absorbed (anonymous) row; ``Message.sender`` is
    ``SET_NULL`` but participations are ``CASCADE``, so without this handler a
    visitor who messaged a seller as a guest loses the membership that lets
    them read the thread at all. Everything below happens in one transaction:

    * authored messages and operator assignments move wholesale;
    * a participation moves unless the survivor already sits in that thread,
      in which case the two read markers are folded (max wins — taking the
      guest's lower mark would re-unread what the survivor has already read)
      and the guest's row is dropped;
    * every ``direct`` thread the guest was in now has a ``direct_key`` built
      from a participant pair that no longer exists, so the key is recomputed
      — and collisions are resolved by folding into the survivor's thread.

    Two different "unknown id" situations, and conflating them loses data: a
    guest who owns nothing here is a genuine no-op, returned quietly; a guest
    who owns chat while the survivor has no user row here yet raises
    :class:`MergeTargetNotReady` so the event is redelivered, because
    returning success would let the outbox mark it delivered and lose the
    person's threads forever.
    """
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from .models import Conversation, ConversationKind, ConversationParticipant, Message

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged event without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    with transaction.atomic():
        # Both reads and the decision they feed happen inside the transaction
        # and before the first write, so the "not yet" path below can never
        # leave half the chat moved.
        try:
            touched_conv_ids = set(
                ConversationParticipant.objects.filter(
                    user_id=from_user_id
                ).values_list("conversation_id", flat=True)
            )
            owns_something = bool(touched_conv_ids) or (
                Message.objects.filter(sender_id=from_user_id).exists()
                or Conversation.objects.filter(
                    assigned_operator_id=from_user_id
                ).exists()
            )
        except (ValueError, TypeError):
            logger.warning("user.merged with unusable user ids: %s", event.event_id)
            return
        if not owns_something:
            # Nothing to carry: the guest never chatted here, or a previous
            # delivery already moved everything. Quiet by design — this is
            # also the at-least-once idempotency path.
            return
        if not get_user_model().objects.filter(pk=into_user_id).exists():
            # The guest HAS chat but the survivor has no row here yet, so
            # nothing can point a FK at them. Not a no-op: raising is this
            # comm layer's retry signal (deliver() wraps it in
            # ActionDeliveryError and the outbox redelivers), so the transfer
            # lands once the survivor's user projection arrives.
            raise MergeTargetNotReady(
                f"user.merged {from_user_id} -> {into_user_id}: the surviving "
                f"account has no user row in stapel-chat yet; redeliver once "
                f"its projection has landed"
            )

        moved_messages = Message.objects.filter(sender_id=from_user_id).update(
            sender_id=into_user_id
        )
        Conversation.objects.filter(assigned_operator_id=from_user_id).update(
            assigned_operator_id=into_user_id
        )
        _merge_participations(from_user_id, into_user_id)
        stale = list(
            Conversation.objects.filter(
                id__in=touched_conv_ids, kind=ConversationKind.DIRECT
            )
        )
        for conv in stale:
            _requalify_direct_key(conv)

    logger.info(
        "user.merged %s -> %s: %s messages, %s conversations touched",
        from_user_id,
        into_user_id,
        moved_messages,
        len(touched_conv_ids),
    )


def _merge_participations(from_user_id, into_user_id) -> None:
    """Move the guest's memberships onto the survivor, folding where both sat
    in the same thread. ``chat_participant_uniq`` forbids two rows for one
    (conversation, user), so a collision is folded rather than reassigned."""
    from .models import ConversationParticipant

    survivor_rows = {
        row.conversation_id: row
        for row in ConversationParticipant.objects.filter(user_id=into_user_id)
    }
    for guest in list(ConversationParticipant.objects.filter(user_id=from_user_id)):
        survivor = survivor_rows.get(guest.conversation_id)
        if survivor is None:
            guest.user_id = into_user_id
            guest.save(update_fields=["user", "updated_at"])
            survivor_rows[guest.conversation_id] = guest
            continue
        # Read markers only ever move forward: fold by max, never by "the
        # merged-away row wins".
        read = max(survivor.last_read_seq, guest.last_read_seq)
        delivered = max(survivor.last_delivered_seq, guest.last_delivered_seq)
        if (read, delivered) != (survivor.last_read_seq, survivor.last_delivered_seq):
            survivor.last_read_seq = read
            survivor.last_delivered_seq = delivered
            survivor.save(
                update_fields=["last_read_seq", "last_delivered_seq", "updated_at"]
            )
        guest.delete()


def _requalify_direct_key(conv) -> None:
    """Recompute a direct thread's ``direct_key`` after its participant set
    changed, resolving a collision by folding into the surviving thread.

    Policy on collision: **the thread that already carried the key keeps it and
    survives**; the stale thread's messages and participants are folded into it
    and the stale row is deleted. The person ends up with one thread per seller,
    which is the whole point of ``chat_conv_uniq_direct``.
    """
    from .models import Conversation, ConversationKind, _direct_key

    user_ids = sorted(
        str(uid) for uid in conv.participants.values_list("user_id", flat=True)
    )
    if len(user_ids) < 2:
        # Both sides of this direct thread turned out to be the same person
        # (the guest had a thread with the very account they merged into).
        # A thread with yourself is dead — same rule the GDPR path applies.
        conv.delete()
        return

    key = _direct_key(
        conv.scope_key, user_ids[0], user_ids[1], conv.subject_type, conv.subject_key
    )
    if key == conv.direct_key:
        return

    winner = (
        Conversation.objects.filter(kind=ConversationKind.DIRECT, direct_key=key)
        .exclude(pk=conv.pk)
        .first()
    )
    if winner is None:
        conv.direct_key = key
        conv.save(update_fields=["direct_key", "updated_at"])
        return
    _fold_conversation(loser=conv, winner=winner)


def _fold_conversation(*, loser, winner) -> None:
    """Move ``loser``'s messages and participants into ``winner``, then delete it.

    ``seq`` is REALLOCATED, appending the folded messages after ``winner``'s
    high-water mark in their existing seq order. An existing seq is never
    rewritten: it is the cursor every client, read marker and replay resumes
    on, so renumbering the surviving thread to interleave the two histories by
    timestamp would silently mark read messages unread and re-deliver old ones.
    The cost is that the folded messages land at the end of the thread rather
    than in wall-clock order; ``created_at`` still carries the true time.
    """
    from .models import Conversation, ConversationParticipant, Message

    winner = Conversation.objects.select_for_update().get(pk=winner.pk)
    taken_client_ids = set(
        Message.objects.filter(conversation_id=winner.pk)
        .exclude(client_msg_id="")
        .values_list("client_msg_id", flat=True)
    )
    seq = winner.last_seq
    for msg in Message.objects.filter(conversation_id=loser.pk).order_by("seq"):
        seq += 1
        msg.conversation_id = winner.pk
        msg.seq = seq
        # rev_seq is the replay cursor: a folded message is new to every client
        # of the surviving thread, so it must land above their cursor.
        msg.rev_seq = seq
        fields = ["conversation", "seq", "rev_seq"]
        if msg.client_msg_id and msg.client_msg_id in taken_client_ids:
            # A send-idempotency key is scoped to the thread it was sent in.
            msg.client_msg_id = ""
            fields.append("client_msg_id")
        msg.save(update_fields=fields)
    if seq != winner.last_seq:
        winner.last_seq = seq
        winner.save(update_fields=["last_seq", "updated_at"])

    winner_user_ids = set(
        ConversationParticipant.objects.filter(
            conversation_id=winner.pk
        ).values_list("user_id", flat=True)
    )
    for part in ConversationParticipant.objects.filter(conversation_id=loser.pk):
        if part.user_id in winner_user_ids:
            # Read markers are NOT folded across a thread boundary: the two
            # rows count in different seq spaces, so the guest's number means
            # nothing here. The folded messages simply arrive unread.
            continue
        part.conversation_id = winner.pk
        part.save(update_fields=["conversation", "updated_at"])
        winner_user_ids.add(part.user_id)
    loser.delete()
