"""A malformed id in an action payload must not become a poison pill.

``user.deleted`` / ``user.merged`` carry ids as strings, and the consumed
contracts' ``format: uuid`` is documentation only — ``jsonschema.validate``
does not enforce ``format`` — so a bad id reaches the handler. Django answers
a key it cannot coerce to the column's type with
``django.core.exceptions.ValidationError``, which is NOT a subclass of
``ValueError``: a guard that catches only ``(ValueError, TypeError)`` lets it
escape, ``consume_actions`` re-raises it to the bus, and the event is
redelivered forever over a payload no retry can repair.

Pinned here: both handlers ACK the malformed payload (return without raising)
and touch no rows.
"""
import types
import uuid

import pytest
from stapel_core.comm import emit
from stapel_core.django.users.models import User

from stapel_chat import services
from stapel_chat.actions import handle_user_deleted, handle_user_merged
from stapel_chat.models import Conversation, ConversationParticipant, Message

pytestmark = pytest.mark.django_db

BAD_IDS = ["not-a-uuid", "", "  ", "42", "['x']"]


def _event(**payload):
    return types.SimpleNamespace(payload=payload, event_id=str(uuid.uuid4()))


@pytest.fixture
def guest(db):
    return User.create_anonymous_user()


@pytest.fixture
def rows(db, guest, other_user):
    """A direct thread the guest actually has content in."""
    conv = services.create_direct(owner=guest, other_user_id=other_user.id)
    services.post_message(conversation=conv, sender=guest, body="is it available?")
    return conv


def _snapshot():
    return (
        sorted(Conversation.objects.values_list("id", "direct_key")),
        sorted(ConversationParticipant.objects.values_list("conversation_id", "user_id")),
        sorted(Message.objects.values_list("id", "conversation_id", "sender_id", "body")),
    )


def test_user_deleted_with_a_malformed_id_acks_and_erases_nothing(rows):
    before = _snapshot()
    for bad in BAD_IDS:
        emit("user.deleted", {"user_id": bad})
    assert _snapshot() == before


def test_user_deleted_without_a_user_id_acks(rows):
    before = _snapshot()
    handle_user_deleted(_event())
    handle_user_deleted(_event(user_id=None))
    assert _snapshot() == before


def test_user_merged_with_a_malformed_id_acks_and_moves_nothing(rows, guest, user):
    """Both directions: a bad *from* id, and — the second door — a bad *into*
    id while the guest genuinely owns chat here."""
    before = _snapshot()
    for bad in BAD_IDS:
        emit("user.merged", {"from_user_id": bad, "into_user_id": str(user.id)})
        emit("user.merged", {"from_user_id": str(guest.id), "into_user_id": bad})
    assert _snapshot() == before


def test_user_merged_without_ids_acks(rows):
    before = _snapshot()
    handle_user_merged(_event())
    handle_user_merged(_event(from_user_id=None, into_user_id=None))
    assert _snapshot() == before


def test_a_wellformed_unknown_user_still_takes_the_quiet_path(rows):
    """A stranger's id is a clean no-op, exactly as before the guard widened."""
    before = _snapshot()
    stranger = str(uuid.uuid4())
    emit("user.deleted", {"user_id": stranger})
    emit("user.merged", {"from_user_id": stranger, "into_user_id": stranger})
    assert _snapshot() == before


def test_a_real_deletion_still_erases(rows, guest):
    """The guard is narrow: a valid id still runs the erasure."""
    emit("user.deleted", {"user_id": str(guest.id)})

    assert not ConversationParticipant.objects.filter(user=guest).exists()
    assert not Message.objects.filter(sender=guest, deleted_at__isnull=True).exists()


def test_unknown_survivor_row_is_not_confused_with_a_bad_id(rows, guest):
    """A survivor id that parses but has no row here still RAISES when the
    guest owns chat — the retry signal must survive the widened guard."""
    from stapel_core.comm.exceptions import ActionDeliveryError

    from stapel_chat.actions import MergeTargetNotReady

    with pytest.raises(ActionDeliveryError) as excinfo:
        emit(
            "user.merged",
            {"from_user_id": str(guest.id), "into_user_id": str(uuid.uuid4())},
        )
    (cause,) = excinfo.value.errors
    assert isinstance(cause, MergeTargetNotReady)
