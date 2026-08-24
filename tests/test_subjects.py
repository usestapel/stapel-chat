"""What a conversation is about — and the identity change that hangs off it.

The load-bearing test in this file is the first one. Through 0.5.x a direct
thread was keyed by the participant PAIR alone and uniquely constrained, so one
buyer and one seller could hold exactly ONE thread however many things they
discussed. stapel-classified could not refuse the second listing — refusing it
would have rendered the wrong card — so it made its own binding append-only,
several subjects per conversation, and marked that table for deletion the day
this shipped. Everything else here exists to make that deletion safe.
"""
import pytest

from stapel_chat import services
from stapel_chat.models import Conversation, ConversationKind, _direct_key
from stapel_chat.subjects import (
    InvalidSubjectPolicy,
    META_MISSING,
    META_OK,
    META_PARTIAL,
    REASON_CARD_MISSING,
    REASON_FAILED,
    REASON_UNREACHABLE,
    REASON_UNREGISTERED,
    get_subject_types,
    register_subject_type,
    reset_subject_types,
    resolve_cards,
    subject_type_names,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_subject_registry():
    from stapel_core.comm import function_registry

    reset_subject_types()
    yield
    reset_subject_types()
    # A function name has exactly one provider, process-wide. Leave the
    # registry as we found it or the next test inherits this one's stand-in.
    function_registry._providers.pop("classified.subject_cards", None)


@pytest.fixture
def listing_type():
    register_subject_type("listing", {"card_function": "classified.subject_cards"})
    return "listing"


# ── The identity change ──────────────────────────────────────────────────


class TestDirectKeyCarriesTheSubject:
    def test_one_pair_two_listings_two_threads(self, user, other_user, listing_type):
        """THE test. Before 0.6.0 these two calls returned the same row, and
        the second listing's conversation rendered the first listing's card."""
        first = services.create_direct(
            owner=user, other_user_id=other_user.id,
            subject_type="listing", subject_key="listing-1",
        )
        second = services.create_direct(
            owner=user, other_user_id=other_user.id,
            subject_type="listing", subject_key="listing-2",
        )

        assert first.id != second.id
        assert first.subject_key == "listing-1"
        assert second.subject_key == "listing-2"

    def test_the_same_pair_and_the_same_subject_is_still_one_thread(
        self, user, other_user, listing_type
    ):
        """Idempotency did not go away — it got a third dimension."""
        first = services.create_direct(
            owner=user, other_user_id=other_user.id,
            subject_type="listing", subject_key="listing-1",
        )
        again = services.create_direct(
            owner=user, other_user_id=other_user.id,
            subject_type="listing", subject_key="listing-1",
        )
        assert first.id == again.id

    def test_who_asked_first_still_does_not_matter(
        self, user, other_user, listing_type
    ):
        """Order-independence over the pair survives the subject."""
        mine = services.create_direct(
            owner=user, other_user_id=other_user.id,
            subject_type="listing", subject_key="listing-1",
        )
        theirs = services.create_direct(
            owner=other_user, other_user_id=user.id,
            subject_type="listing", subject_key="listing-1",
        )
        assert mine.id == theirs.id

    def test_a_subjectless_key_is_byte_identical_to_the_old_one(self):
        """The compatibility hinge, asserted rather than trusted.

        Every conversation that existed before this release has a key computed
        by the OLD function. If the new one spelled a subject-less key even
        slightly differently, `create_direct` would stop finding those rows
        and quietly open a second thread beside every live one. The subject
        segments are appended only when there is a subject, and this is what
        says so.
        """
        assert _direct_key("", "a", "b") == "\x1fa\x1fb"
        assert _direct_key("ws", "a", "b") == "ws\x1fa\x1fb"
        # Unchanged from 0.5.x for the no-subject case, and only then.
        assert _direct_key("ws", "a", "b", "listing", "1") != _direct_key("ws", "a", "b")

    def test_an_existing_threadless_pair_keeps_its_thread(self, user, other_user):
        """A pair who already talk keep the thread they have: it is simply
        the thread about nothing in particular, and an unchanged call still
        returns it."""
        before = services.create_direct(owner=user, other_user_id=other_user.id)
        after = services.create_direct(owner=user, other_user_id=other_user.id)
        assert before.id == after.id
        assert before.subject_type == ""

    def test_a_subject_thread_is_not_the_pairs_old_thread(
        self, user, other_user, listing_type
    ):
        """And the first subject-bearing contact after the upgrade opens a NEW
        thread rather than adopting the old one. That is the migration story
        the CHANGELOG has to be loud about — it is a deliberate consequence,
        not an accident."""
        plain = services.create_direct(owner=user, other_user_id=other_user.id)
        about = services.create_direct(
            owner=user, other_user_id=other_user.id,
            subject_type="listing", subject_key="listing-1",
        )
        assert plain.id != about.id

    def test_the_unique_constraint_still_holds_per_subject(
        self, user, other_user, listing_type
    ):
        key = _direct_key("", user.pk, other_user.id, "listing", "listing-1")
        services.create_direct(
            owner=user, other_user_id=other_user.id,
            subject_type="listing", subject_key="listing-1",
        )
        assert (
            Conversation.objects.filter(
                kind=ConversationKind.DIRECT, direct_key=key
            ).count()
            == 1
        )


# ── The registry ─────────────────────────────────────────────────────────


class TestTheRegistry:
    def test_it_ships_empty(self):
        """A messaging engine has no subject types of its own, and `listing`
        belongs to whoever owns listings."""
        assert subject_type_names() == ()

    def test_settings_then_runtime_wins(self, settings, listing_type):
        settings.STAPEL_CHAT = {
            "SUBJECT_TYPES": {"listing": {"card_function": "from.settings"}}
        }
        register_subject_type("listing", {"card_function": "from.runtime"})
        assert get_subject_types()["listing"]["card_function"] == "from.runtime"

    def test_none_removes_a_type(self, settings):
        settings.STAPEL_CHAT = {
            "SUBJECT_TYPES": {"listing": {"card_function": "x.y"}}
        }
        assert "listing" in get_subject_types()
        register_subject_type("listing", None)
        assert "listing" not in get_subject_types()

    def test_a_policy_is_completed_from_the_defaults(self, listing_type):
        policy = get_subject_types()["listing"]
        assert policy["request_field"] == "keys"
        assert policy["response_field"] == "cards"

    def test_a_policy_without_a_card_function_is_refused(self):
        """A subject nothing can render is a string in a database."""
        with pytest.raises(InvalidSubjectPolicy):
            register_subject_type("listing", {"label": "chat.subject.listing"})

    def test_an_unregistered_type_cannot_be_stored(self, user, other_user):
        """Refused at the door, not discovered when somebody opens the thread."""
        with pytest.raises(services.UnknownSubjectType):
            services.create_direct(
                owner=user, other_user_id=other_user.id,
                subject_type="listing", subject_key="listing-1",
            )

    def test_half_a_subject_is_refused(self, user, other_user, listing_type):
        with pytest.raises(services.IncompleteSubject):
            services.create_direct(
                owner=user, other_user_id=other_user.id, subject_type="listing"
            )
        with pytest.raises(services.IncompleteSubject):
            services.create_direct(
                owner=user, other_user_id=other_user.id, subject_key="listing-1"
            )


# ── Card resolution ──────────────────────────────────────────────────────


class TestCards:
    def test_one_call_per_type_for_a_whole_page(self, listing_type):
        """The reason the provider contract is a batch: fifty conversations
        about fifty listings cost one round trip, not fifty."""
        from stapel_core.comm import function

        calls = []

        @function("classified.subject_cards")
        def _cards(payload):
            calls.append(sorted(payload["keys"]))
            return {"cards": {k: {"title": k} for k in payload["keys"]}}

        out = resolve_cards(
            [("listing", "a"), ("listing", "b"), ("listing", "c")]
        )
        assert len(calls) == 1
        assert calls[0] == ["a", "b", "c"]
        assert out[("listing", "a")]["card"] == {"title": "a"}
        assert out[("listing", "a")]["meta_status"] == META_OK

    def test_a_gone_card_is_a_card(self, listing_type):
        """classified answers a deleted listing with a `gone` card rather than
        omitting the key. Chat passes it straight through — it does not know
        what `gone` means and must not learn."""
        from stapel_core.comm import function

        @function("classified.subject_cards")
        def _cards(payload):
            return {"cards": {k: {"state": "gone"} for k in payload["keys"]}}

        out = resolve_cards([("listing", "dead")])
        assert out[("listing", "dead")]["card"] == {"state": "gone"}
        assert out[("listing", "dead")]["meta_status"] == META_OK

    def test_an_omitted_key_is_a_provider_defect_not_an_absent_subject(
        self, listing_type
    ):
        """The provider's contract says it answers for every key. A key it
        drops is reported as degraded, because rendering it as "no subject"
        would hide a broken provider behind a plausible-looking header."""
        from stapel_core.comm import function

        @function("classified.subject_cards")
        def _cards(payload):
            return {"cards": {}}

        out = resolve_cards([("listing", "x")])
        assert out[("listing", "x")]["card"] is None
        assert out[("listing", "x")]["meta_reason"] == REASON_CARD_MISSING

    def test_a_failing_provider_degrades_the_header_not_the_thread(
        self, listing_type
    ):
        """A conversation never fails to open because a catalogue blinked."""
        from stapel_core.comm import function

        @function("classified.subject_cards")
        def _cards(payload):
            raise RuntimeError("catalogue down")

        out = resolve_cards([("listing", "x")])
        assert out[("listing", "x")]["meta_status"] == META_PARTIAL
        assert out[("listing", "x")]["meta_reason"] == REASON_FAILED

    def test_an_unregistered_function_is_named_as_unreachable(self, listing_type):
        out = resolve_cards([("listing", "x")])
        assert out[("listing", "x")]["meta_reason"] == REASON_UNREACHABLE

    def test_a_type_no_registry_provides_says_so(self):
        out = resolve_cards([("ghost", "x")])
        assert out[("ghost", "x")]["meta_status"] == META_MISSING
        assert out[("ghost", "x")]["meta_reason"] == REASON_UNREGISTERED

    def test_a_subjectless_conversation_asks_nobody_anything(
        self, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        assert services.subject_cards_for([conv]) == {}
