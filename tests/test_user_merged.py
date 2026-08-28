"""``user.merged`` — a guest's threads survive signing in.

stapel-auth absorbs an anonymous account into an existing one and then DELETES
the guest row; ``ConversationParticipant`` hangs off it by CASCADE, so without
this handler a visitor who messaged a seller as a guest loses the membership
that lets them read the thread at all. What is pinned here:

* the walk — guest and seller talk, the merge lands, the survivor is the
  participant, reads the history over REST, and the thread's ``direct_key``
  matches the NEW pair;
* the ``chat_participant_uniq`` collision: read markers fold by ``max()``,
  never by "the merged-away row wins";
* the ``chat_conv_uniq_direct`` collision: the survivor's own thread keeps the
  key and the guest's thread is folded into it, messages appended after the
  high-water ``seq`` (an existing seq is never rewritten);
* idempotency under at-least-once delivery.
"""
import pytest
from stapel_core.comm import emit
from stapel_core.django.users.models import User

from stapel_chat import services
from stapel_chat.actions import MergeTargetNotReady
from stapel_chat.models import (
    Conversation,
    ConversationKind,
    ConversationParticipant,
    Message,
    _direct_key,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def guest(db):
    return User.create_anonymous_user()


def _merge(from_user, into_user):
    emit(
        "user.merged",
        {
            "from_user_id": str(from_user.id),
            "into_user_id": str(into_user.id),
            "reason": "anonymous_promotion",
        },
    )


def _participant_ids(conv):
    return set(conv.participants.values_list("user_id", flat=True))


# ── the walk ────────────────────────────────────────────────────────────


def test_guest_thread_carries_over_to_the_survivor(api_client, guest, user, other_user):
    """other_user is the seller; ``user`` is the account the guest signs in to."""
    conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    services.post_message(conversation=conv, sender=guest, body="is it still available?")
    services.post_message(conversation=conv, sender=other_user, body="yes")

    _merge(guest, user)

    conv.refresh_from_db()
    assert _participant_ids(conv) == {user.id, other_user.id}
    assert conv.direct_key == _direct_key("", user.id, other_user.id)
    assert not ConversationParticipant.objects.filter(user=guest).exists()
    assert Message.objects.filter(sender=user).count() == 1

    # The survivor reads the history through the endpoint a browser calls.
    api_client.force_authenticate(user=user)
    resp = api_client.get(f"/chat/api/v1/conversations/{conv.id}/messages")
    assert resp.status_code == 200, resp.content
    bodies = [row["body"] for row in resp.json()["items"]]
    assert "is it still available?" in bodies and "yes" in bodies


def test_recomputed_key_finds_the_thread_on_the_next_create_direct(guest, user, other_user):
    """The key is not cosmetic: create_direct must land on the same thread."""
    conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    _merge(guest, user)
    assert services.create_direct(owner=user, other_user_id=other_user.id).id == conv.id
    assert Conversation.objects.filter(kind=ConversationKind.DIRECT).count() == 1


def test_assigned_operator_is_reassigned(guest, user):
    conv = services.create_support(customer=user)
    Conversation.objects.filter(pk=conv.pk).update(assigned_operator=guest)

    _merge(guest, user)

    conv.refresh_from_db()
    assert conv.assigned_operator_id == user.id


# ── participant collision ───────────────────────────────────────────────


def test_participant_collision_folds_read_marks_by_max(guest, user, other_user):
    conv = services.create_group(owner=other_user, participant_ids=[guest.id, user.id])
    for _ in range(5):
        services.post_message(conversation=conv, sender=other_user, body="hi")

    ConversationParticipant.objects.filter(conversation=conv, user=guest).update(
        last_read_seq=2, last_delivered_seq=5
    )
    ConversationParticipant.objects.filter(conversation=conv, user=user).update(
        last_read_seq=4, last_delivered_seq=4
    )

    _merge(guest, user)

    rows = ConversationParticipant.objects.filter(conversation=conv)
    assert rows.count() == 2  # the guest's row is gone, not duplicated
    survivor = rows.get(user=user)
    assert survivor.last_read_seq == 4  # the guest's lower mark did not win
    assert survivor.last_delivered_seq == 5  # the guest's higher mark did


def test_no_collision_reassigns_the_row_in_place(guest, user, other_user):
    conv = services.create_group(owner=other_user, participant_ids=[guest.id])
    row_id = ConversationParticipant.objects.get(conversation=conv, user=guest).pk

    _merge(guest, user)

    assert ConversationParticipant.objects.get(pk=row_id).user_id == user.id


# ── direct_key collision ────────────────────────────────────────────────


def test_direct_key_collision_folds_the_guest_thread_into_the_survivors(
    guest, user, other_user
):
    """Policy: the survivor's own thread keeps the key and survives; the
    guest's thread is folded into it and deleted."""
    survivor_conv = services.create_direct(owner=user, other_user_id=other_user.id)
    services.post_message(conversation=survivor_conv, sender=user, body="mine 1")
    services.post_message(conversation=survivor_conv, sender=other_user, body="mine 2")

    guest_conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    services.post_message(conversation=guest_conv, sender=guest, body="guest 1")
    services.post_message(conversation=guest_conv, sender=other_user, body="guest 2")

    assert Conversation.objects.filter(kind=ConversationKind.DIRECT).count() == 2

    _merge(guest, user)

    assert Conversation.objects.filter(kind=ConversationKind.DIRECT).count() == 1
    assert not Conversation.objects.filter(pk=guest_conv.pk).exists()

    survivor_conv.refresh_from_db()
    assert survivor_conv.direct_key == _direct_key("", user.id, other_user.id)
    assert _participant_ids(survivor_conv) == {user.id, other_user.id}

    rows = list(
        Message.objects.filter(conversation=survivor_conv).order_by("seq").values(
            "body", "seq", "rev_seq"
        )
    )
    # Appended after the high-water mark, in their original order; the
    # survivor's own seqs 1 and 2 are untouched.
    assert [r["body"] for r in rows] == ["mine 1", "mine 2", "guest 1", "guest 2"]
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]
    # rev_seq of a folded row is its new seq, so a resuming client sees them.
    assert [r["rev_seq"] for r in rows[2:]] == [3, 4]
    survivor_conv.refresh_from_db()
    assert survivor_conv.last_seq == 4


def test_folded_thread_keeps_the_survivors_read_marks(guest, user, other_user):
    survivor_conv = services.create_direct(owner=user, other_user_id=other_user.id)
    services.post_message(conversation=survivor_conv, sender=other_user, body="mine")
    services.mark_read(conversation=survivor_conv, user=user, upto_seq=1)

    guest_conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    services.post_message(conversation=guest_conv, sender=other_user, body="guest")
    services.mark_read(conversation=guest_conv, user=guest, upto_seq=1)

    _merge(guest, user)

    row = ConversationParticipant.objects.get(conversation=survivor_conv, user=user)
    # Seq spaces differ across threads, so the guest's "1" is not folded in:
    # the folded message arrives unread rather than silently pre-read.
    assert row.last_read_seq == 1
    assert (
        services.unread_count(conversation=survivor_conv, participant=row) == 1
    )


def test_direct_thread_between_guest_and_survivor_is_deleted(guest, user):
    """After the merge both sides of that thread are the same person."""
    conv = services.create_direct(owner=guest, other_user_id=user.id)
    services.post_message(conversation=conv, sender=guest, body="note to self")

    _merge(guest, user)

    assert not Conversation.objects.filter(pk=conv.pk).exists()


# ── idempotency / no-ops ────────────────────────────────────────────────


def test_second_delivery_changes_nothing(guest, user, other_user):
    conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    services.post_message(conversation=conv, sender=guest, body="hello")

    _merge(guest, user)
    snapshot = (
        list(Conversation.objects.values_list("id", "direct_key", "last_seq")),
        list(Message.objects.values_list("id", "conversation_id", "seq", "sender_id")),
        list(ConversationParticipant.objects.values_list("id", "conversation_id", "user_id")),
    )

    _merge(guest, user)  # at-least-once delivery

    assert (
        list(Conversation.objects.values_list("id", "direct_key", "last_seq")),
        list(Message.objects.values_list("id", "conversation_id", "seq", "sender_id")),
        list(ConversationParticipant.objects.values_list("id", "conversation_id", "user_id")),
    ) == snapshot


def test_second_delivery_of_a_folded_merge_changes_nothing(guest, user, other_user):
    survivor_conv = services.create_direct(owner=user, other_user_id=other_user.id)
    services.post_message(conversation=survivor_conv, sender=user, body="mine")
    guest_conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    services.post_message(conversation=guest_conv, sender=guest, body="guest")

    _merge(guest, user)
    snapshot = list(Message.objects.values_list("id", "conversation_id", "seq"))

    _merge(guest, user)

    assert list(Message.objects.values_list("id", "conversation_id", "seq")) == snapshot
    assert Conversation.objects.count() == 1


def test_guest_with_no_chat_is_a_clean_no_op(guest, user):
    _merge(guest, user)
    assert Conversation.objects.count() == 0


def test_guest_with_no_chat_and_an_unknown_survivor_stays_quiet(guest):
    """No threads to carry — a genuine no-op, and the retry loop must not start."""
    import uuid

    emit(
        "user.merged",
        {
            "from_user_id": str(guest.id),
            "into_user_id": str(uuid.uuid4()),
            "reason": "anonymous_promotion",
        },
    )
    assert Conversation.objects.count() == 0


def test_second_delivery_after_a_completed_merge_never_raises(guest, user, other_user):
    """Post-merge the guest owns nothing, so redelivery takes the quiet path."""
    services.create_direct(owner=guest, other_user_id=other_user.id)

    _merge(guest, user)
    _merge(guest, user)  # must not raise MergeTargetNotReady

    assert ConversationParticipant.objects.filter(user=user).exists()


# ── the survivor has not been projected here yet ────────────────────────


def test_unknown_survivor_raises_and_moves_nothing(guest, other_user):
    """The guest HAS threads: returning success would let the outbox mark the
    event delivered and lose them forever. Raise so it is redelivered."""
    import uuid

    from stapel_core.comm.exceptions import ActionDeliveryError

    conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    services.post_message(conversation=conv, sender=guest, body="hello")
    key_before = conv.direct_key
    survivor_id = uuid.uuid4()

    with pytest.raises(ActionDeliveryError) as excinfo:
        emit(
            "user.merged",
            {
                "from_user_id": str(guest.id),
                "into_user_id": str(survivor_id),
                "reason": "anonymous_promotion",
            },
        )

    (cause,) = excinfo.value.errors
    assert isinstance(cause, MergeTargetNotReady)
    # An operator staring at a redelivery loop can name both accounts.
    assert str(guest.id) in str(cause) and str(survivor_id) in str(cause)

    # Nothing half-moved: a redelivery finds the thread intact under the guest.
    conv.refresh_from_db()
    assert _participant_ids(conv) == {guest.id, other_user.id}
    assert conv.direct_key == key_before
    assert Message.objects.filter(sender=guest).count() == 1


def test_redelivery_after_the_survivor_appears_completes_the_transfer(guest, other_user):
    """The raise is a real retry path, not just a louder failure."""
    import uuid

    from stapel_core.comm.exceptions import ActionDeliveryError

    conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    services.post_message(conversation=conv, sender=guest, body="hello")
    survivor_id = uuid.uuid4()
    payload = {
        "from_user_id": str(guest.id),
        "into_user_id": str(survivor_id),
        "reason": "anonymous_promotion",
    }

    with pytest.raises(ActionDeliveryError):
        emit("user.merged", payload)

    # The survivor's user projection lands...
    survivor = User.objects.create(
        id=survivor_id, username="late", email="late@example.com"
    )

    emit("user.merged", payload)  # ...and the outbox redelivers.

    conv.refresh_from_db()
    assert _participant_ids(conv) == {survivor.id, other_user.id}
    assert conv.direct_key == _direct_key("", survivor.id, other_user.id)
    assert Message.objects.filter(sender=survivor).count() == 1


def test_merge_into_self_is_a_no_op(guest, other_user):
    conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    _merge(guest, guest)
    conv.refresh_from_db()
    assert _participant_ids(conv) == {guest.id, other_user.id}


def test_committed_schema_is_enforced_on_delivery():
    """The harness registers schemas/consumes/, so a payload that does not
    match the committed contract never reaches the handler."""
    with pytest.raises(Exception):
        emit("user.merged", {"from_user_id": "0f9a2f7e-0000-4000-8000-000000000001"})
