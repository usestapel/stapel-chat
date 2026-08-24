"""The two reads a consumer had to fake: "a thread was opened" and "who is in it".

Both gaps had the same shape — chat knew something, exposed no way to ask, and
the consumer kept its own copy. stapel-classified's conversation binding was
client-driven purely because no `chat.conversation.created` existed, and it
stored `initiator_id`/`counterparty_id` on its own row purely because nothing
answered "is this user a party to this conversation". A copy that nobody can
refresh is a copy that goes stale.
"""
import uuid

import pytest
from stapel_core.comm import action_registry, call, subscribe_action

from stapel_chat import services
from stapel_chat.subjects import register_subject_type, reset_subject_types

pytestmark = pytest.mark.django_db

EVENT = "chat.conversation.created"


@pytest.fixture
def created_events():
    collected = []

    def _handler(event):
        collected.append(event)

    subscribe_action(EVENT, _handler)
    try:
        yield collected
    finally:
        handlers = action_registry._subscribers.get(EVENT, [])
        if _handler in handlers:
            handlers.remove(_handler)


@pytest.fixture(autouse=True)
def _clean_subjects():
    reset_subject_types()
    yield
    reset_subject_types()


# ── chat.conversation.created ────────────────────────────────────────────


class TestCreatedEmit:
    def test_opening_a_thread_announces_it(self, user, other_user, created_events):
        """Before this, a consumer learned a conversation existed only when
        its first message arrived — so binding a thread to a domain object had
        to be driven by the client that created it, in a fleet that is
        otherwise server-authoritative about exactly this."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)

        assert len(created_events) == 1
        payload = created_events[0].payload
        assert payload["conversation_id"] == str(conv.id)
        assert payload["kind"] == "direct"
        assert payload["creator_id"] == str(user.pk)
        assert sorted(payload["participant_ids"]) == sorted(
            [str(user.pk), str(other_user.pk)]
        )

    def test_an_idempotent_create_is_not_a_second_creation(
        self, user, other_user, created_events
    ):
        """The rule a consumer's binding depends on: exactly one event per
        thread. A consumer that bound a domain object per idempotent retry
        would be right to double-bind, and it would be this module's fault."""
        services.create_direct(owner=user, other_user_id=other_user.id)
        services.create_direct(owner=user, other_user_id=other_user.id)

        assert len(created_events) == 1

    def test_the_subject_rides_along(self, user, other_user, created_events):
        register_subject_type("listing", {"card_function": "classified.subject_cards"})
        services.create_direct(
            owner=user, other_user_id=other_user.id,
            subject_type="listing", subject_key="listing-7",
        )
        payload = created_events[0].payload
        assert payload["subject_type"] == "listing"
        assert payload["subject_key"] == "listing-7"

    def test_group_and_support_announce_themselves_too(
        self, user, other_user, created_events
    ):
        services.create_group(owner=user, participant_ids=[other_user.id])
        services.create_support(customer=user)

        kinds = [e.payload["kind"] for e in created_events]
        assert kinds == ["group", "support"]

    def test_a_second_listing_gets_its_own_event(
        self, user, other_user, created_events
    ):
        """The identity change, seen from the consumer's side: two subjects,
        two threads, two events to bind — no append-only table needed."""
        register_subject_type("listing", {"card_function": "classified.subject_cards"})
        for key in ("l-1", "l-2"):
            services.create_direct(
                owner=user, other_user_id=other_user.id,
                subject_type="listing", subject_key=key,
            )

        assert len(created_events) == 2
        assert len({e.payload["conversation_id"] for e in created_events}) == 2


# ── chat.conversation_participants ───────────────────────────────────────


class TestParticipantsRead:
    def test_it_answers_who_is_a_party(self, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)

        answer = call(
            "chat.conversation_participants", {"conversation_ids": [str(conv.id)]}
        )
        row = answer["conversations"][str(conv.id)]

        assert row["exists"] is True
        assert row["kind"] == "direct"
        assert sorted(p["user_id"] for p in row["participants"]) == sorted(
            [str(user.pk), str(other_user.pk)]
        )

    def test_it_is_a_batch(self, user, other_user):
        a = services.create_direct(owner=user, other_user_id=other_user.id)
        b = services.create_group(owner=user, participant_ids=[other_user.id])

        answer = call(
            "chat.conversation_participants",
            {"conversation_ids": [str(a.id), str(b.id)]},
        )
        assert set(answer["conversations"]) == {str(a.id), str(b.id)}

    def test_an_id_that_names_nothing_is_answered_not_dropped(self):
        """The same rule classified.subject_cards follows for a deleted
        listing: the caller is holding that id for a reason and needs to be
        told it is dead, not left to infer it from an absence."""
        ghost = str(uuid.uuid4())
        answer = call(
            "chat.conversation_participants", {"conversation_ids": [ghost]}
        )
        assert answer["conversations"][ghost]["exists"] is False
        assert answer["conversations"][ghost]["participants"] == []

    def test_a_malformed_id_is_not_found_not_a_500(self):
        answer = call(
            "chat.conversation_participants", {"conversation_ids": ["not-a-uuid"]}
        )
        assert answer["conversations"]["not-a-uuid"]["exists"] is False

    def test_it_carries_the_subject_so_a_consumer_can_stop_storing_it(
        self, user, other_user
    ):
        """What makes the other module's row deletable: everything it was
        keeping locally — both parties AND the subject — is answerable here."""
        register_subject_type("listing", {"card_function": "classified.subject_cards"})
        conv = services.create_direct(
            owner=user, other_user_id=other_user.id,
            subject_type="listing", subject_key="listing-9",
        )

        row = call(
            "chat.conversation_participants", {"conversation_ids": [str(conv.id)]}
        )["conversations"][str(conv.id)]

        assert row["subject_type"] == "listing"
        assert row["subject_key"] == "listing-9"

    def test_an_operator_is_named_by_role(self, user, operator_user):
        conv = services.create_support(customer=user)
        services.assign_operator(conversation=conv, operator=operator_user)

        row = call(
            "chat.conversation_participants", {"conversation_ids": [str(conv.id)]}
        )["conversations"][str(conv.id)]
        roles = {p["user_id"]: p["role"] for p in row["participants"]}
        assert roles[str(operator_user.pk)] == "operator"

    def test_asking_about_nothing_answers_nothing(self):
        assert call("chat.conversation_participants", {"conversation_ids": []}) == {
            "conversations": {}
        }
