"""Block enforcement at send — built against a provider by NAME.

stapel-profiles owns blocks and is being finished in parallel with this
release; `profiles.relationships` may or may not exist in a given deployment
on the day chat ships. That is exactly why nothing here imports it. The tests
stand up a stand-in under the CONFIGURED NAME, which is the whole contract:
`{"pairs": [[a, b], ...]}` in, `{"blocked": [[a, b], ...]}` out, blocked in
either direction.

The three rules that must never bend, each with a test:

1. A refusal discloses nothing — not the direction, not that a block exists.
2. A provider that is present and FAILING answers 503, never "allowed".
   An outage is not consent.
3. A provider that is ABSENT is not the same as one that is failing, and the
   difference is a deployment's declared enforcement mode, announced at boot.
"""
import pytest
from stapel_core.comm import function, function_registry

from stapel_chat import services
from stapel_chat.blocks import (
    BlockCheckUnavailable,
    blocked_pairs,
    enforcement_mode,
    is_blocked,
)

pytestmark = pytest.mark.django_db

BLOCK_FUNCTION = "profiles.relationships"


@pytest.fixture(autouse=True)
def _clean_provider():
    yield
    function_registry._providers.pop(BLOCK_FUNCTION, None)


@pytest.fixture
def blocking(request):
    """Register a stand-in for `profiles.relationships` that blocks the pairs
    the test names. Registered under the CONFIGURED name — the same string a
    real stapel-profiles will claim — so this proves the wiring, not a mock."""
    def _install(blocked=(), fail=False):
        calls = []

        @function(BLOCK_FUNCTION)
        def _relationships(payload):
            calls.append(payload)
            if fail:
                raise RuntimeError("profiles is down")
            pairs = {frozenset(map(str, p)) for p in blocked}
            return {
                "blocked": [
                    p for p in payload["pairs"] if frozenset(map(str, p)) in pairs
                ]
            }

        return calls
    return _install


def _direct(user, other):
    return services.create_direct(owner=user, other_user_id=other.id)


# ── The refusal ──────────────────────────────────────────────────────────


def test_a_blocked_party_cannot_send_into_an_existing_thread(
    user, other_user, blocking
):
    """The gap this closes. Elsewhere in the fleet a block stops a NEW
    conversation; nothing stopped the next message in one that already
    existed, which is half a block."""
    conv = _direct(user, other_user)
    blocking(blocked=[(user.pk, other_user.pk)])

    with pytest.raises(services.SendRefused):
        services.post_message(conversation=conv, sender=user, body="hello?")


def test_the_block_holds_in_either_direction(user, other_user, blocking):
    """Whoever set it, neither may send. `blocked_pairs` answers in frozensets
    precisely so no caller has to remember which way round it asked — and so
    direction is unreportable by construction rather than by discipline."""
    conv = _direct(user, other_user)
    blocking(blocked=[(other_user.pk, user.pk)])

    with pytest.raises(services.SendRefused):
        services.post_message(conversation=conv, sender=user, body="still here")
    with pytest.raises(services.SendRefused):
        services.post_message(conversation=conv, sender=other_user, body="and me")


def test_the_refusal_tells_the_sender_nothing(user, other_user, blocking):
    """Rule 1, asserted on the exception itself. A refusal that carried a
    reason would eventually carry it into a response body, and telling the
    blocked party 'they blocked you' turns a quiet boundary into a
    notification."""
    conv = _direct(user, other_user)
    blocking(blocked=[(user.pk, other_user.pk)])

    with pytest.raises(services.SendRefused) as caught:
        services.post_message(conversation=conv, sender=user, body="hi")

    assert str(caught.value) == ""
    assert not getattr(caught.value, "args", ())
    # And nothing on it names the other party or the direction.
    assert not [a for a in dir(caught.value) if a in ("reason", "blocker", "direction")]


def test_an_unblocked_send_still_works(user, other_user, blocking):
    conv = _direct(user, other_user)
    blocking(blocked=[])
    msg = services.post_message(conversation=conv, sender=user, body="hello")
    assert msg.body == "hello"


# ── Rule 2: an outage is not consent ─────────────────────────────────────


def test_a_failing_provider_is_a_server_error_never_an_allowed_message(
    user, other_user, blocking
):
    """The precedent is stapel-classified's BlockCheckUnavailable -> 503, kept
    identical so the two modules cannot disagree about what an unreachable
    block store means. Failing OPEN here would deliver a message to somebody
    who blocked the sender, and they would never know why."""
    conv = _direct(user, other_user)
    blocking(fail=True)

    with pytest.raises(BlockCheckUnavailable):
        services.post_message(conversation=conv, sender=user, body="hello")


def test_unavailable_is_not_a_refusal(user, other_user, blocking):
    """A 503 must not be catchable as a 403. Answering 'refused' for an outage
    tells a sender they are blocked when in fact a service is down."""
    conv = _direct(user, other_user)
    blocking(fail=True)

    with pytest.raises(BlockCheckUnavailable):
        services.post_message(conversation=conv, sender=user, body="hello")
    assert not issubclass(BlockCheckUnavailable, services.ChatError)
    assert not issubclass(BlockCheckUnavailable, services.SendRefused)


def test_required_enforcement_refuses_to_run_without_a_provider(
    user, other_user, settings
):
    """A deployment that HAS blocks and declares it will not proceed without
    one. No provider is registered here at all."""
    settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "required"}
    conv = _direct(user, other_user)

    with pytest.raises(BlockCheckUnavailable):
        services.post_message(conversation=conv, sender=user, body="hello")


# ── Rule 3: absent is not failing ────────────────────────────────────────


def test_auto_with_no_provider_at_all_delivers(user, other_user):
    """The fleet today: stapel-profiles may not be mounted, and chat must
    still be deployable ahead of it. Announced at every boot by W003 — this is
    never the first time anybody hears it."""
    conv = _direct(user, other_user)
    msg = services.post_message(conversation=conv, sender=user, body="hello")
    assert msg.body == "hello"


def test_off_asks_nobody_anything(user, other_user, settings, blocking):
    settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "off"}
    conv = _direct(user, other_user)
    calls = blocking(blocked=[(user.pk, other_user.pk)])

    services.post_message(conversation=conv, sender=user, body="hello")
    assert calls == []


def test_an_unknown_mode_reads_as_auto(settings):
    settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "sure-why-not"}
    assert enforcement_mode() == "auto"


# ── Which threads ────────────────────────────────────────────────────────


def test_a_group_room_is_not_checked(user, other_user, blocking):
    """A block is a fact between two people; a group room is somebody else's
    convening, and silently dropping one member's messages out of it is a
    different product with a UI obligation this module cannot meet."""
    conv = services.create_group(owner=user, participant_ids=[other_user.id])
    calls = blocking(blocked=[(user.pk, other_user.pk)])

    services.post_message(conversation=conv, sender=user, body="hello all")
    assert calls == []


def test_support_is_never_checked(user, blocking):
    """An operator is not a peer. A customer who blocked an agent would
    otherwise have muted the help desk."""
    conv = services.create_support(customer=user)
    calls = blocking(blocked=[])

    services.post_message(conversation=conv, sender=user, body="help")
    assert calls == []


def test_a_system_line_has_no_sender_to_block(user, other_user, blocking):
    conv = _direct(user, other_user)
    calls = blocking(blocked=[(user.pk, other_user.pk)])

    services.post_message(conversation=conv, sender=None, kind="system", body="joined")
    assert calls == []


# ── The wire contract, by name ───────────────────────────────────────────


def test_the_provider_is_asked_in_the_shape_profiles_serves(
    user, other_user, blocking
):
    """`{"pairs": [[a, b], ...]}` in. Asserted here because chat must build
    against the name and the shape without importing stapel-profiles — the
    two modules stay independently deployable, and this is the only place the
    agreement is written down on chat's side."""
    conv = _direct(user, other_user)
    calls = blocking(blocked=[])

    services.post_message(conversation=conv, sender=user, body="hello")

    assert len(calls) == 1
    assert set(calls[0]) == {"pairs"}
    assert calls[0]["pairs"] == [[str(user.pk), str(other_user.pk)]]


def test_blocked_pairs_answers_in_direction_free_sets(user, other_user, blocking):
    blocking(blocked=[(user.pk, other_user.pk)])
    hits = blocked_pairs([(user.pk, other_user.pk)])
    assert hits == {frozenset((str(user.pk), str(other_user.pk)))}
    assert is_blocked(other_user.pk, user.pk) is True


def test_a_pair_with_itself_is_never_asked_about(user, blocking):
    calls = blocking(blocked=[])
    assert blocked_pairs([(user.pk, user.pk)]) == set()
    assert calls == []
