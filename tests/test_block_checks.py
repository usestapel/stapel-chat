"""`manage.py check` says out loud, at every boot, whether blocks are enforced.

"Blocks are not enforced in this deployment" is a sentence an operator must
read, never something a user discovers by successfully messaging somebody who
blocked them. All three enforcement states are announced — including the
deliberate `off`, because a decision on the record is not a defect but an
undeclared one is.
"""
import pytest
from stapel_core.comm import function, function_registry

from stapel_chat.checks import check_block_enforcement, check_subject_types
from stapel_chat.subjects import register_subject_type, reset_subject_types

BLOCK_FUNCTION = "profiles.relationships"


@pytest.fixture(autouse=True)
def _clean():
    reset_subject_types()
    yield
    reset_subject_types()
    function_registry._providers.pop(BLOCK_FUNCTION, None)
    function_registry._providers.pop("classified.subject_cards", None)


def _ids(issues):
    return [i.id for i in issues]


@pytest.fixture
def provider():
    @function(BLOCK_FUNCTION)
    def _relationships(payload):
        return {"blocked": []}


class TestBlockChecks:
    def test_auto_without_a_provider_warns(self, settings):
        """The fleet's state today: stapel-profiles may not be mounted, and
        chat must be deployable ahead of it — loudly, not quietly."""
        settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "auto"}
        assert _ids(check_block_enforcement(None)) == ["stapel_chat.W003"]

    def test_auto_with_a_provider_is_silent(self, settings, provider):
        settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "auto"}
        assert check_block_enforcement(None) == []

    def test_required_without_a_provider_is_an_error(self, settings):
        """A deployment that HAS blocks and declares it does not boot without
        one — the send path would answer 503 anyway, and a broken deployment
        should say so at check time rather than at the first message."""
        settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "required"}
        assert _ids(check_block_enforcement(None)) == ["stapel_chat.E017"]

    def test_e017_names_the_floor_and_what_it_cannot_see(self, settings):
        """The two sentences an operator on a bus fleet needs (0.6.2).

        A green boot is not evidence over a bus transport — the subject IS the
        function name and there is no route table, so this check cannot see a
        stale remote provider and must say so rather than imply otherwise.
        And the floor is a version, not a package name: a profiles one release
        too old fails at the first call, not here.
        """
        settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "required"}
        hint = check_block_enforcement(None)[0].hint
        assert "stapel-profiles >= 0.16.0" in hint
        assert "CANNOT see a stale remote provider" in hint
        assert "DEPLOYED, not merely" in hint

    def test_required_with_a_provider_is_silent(self, settings, provider):
        settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "required"}
        assert check_block_enforcement(None) == []

    def test_off_is_announced_even_though_it_is_a_choice(self, settings, provider):
        """A choice on the record. It still gets said out loud every boot: a
        block that stops nothing is exactly the thing a deployment otherwise
        learns about from a user complaint."""
        settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "off"}
        assert _ids(check_block_enforcement(None)) == ["stapel_chat.W004"]

    def test_an_unrecognized_mode_is_an_error_not_a_silent_default(self, settings):
        """It reads as 'auto' at runtime, and that is not a thing to leave
        implicit for a security control."""
        settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "sure-why-not"}
        assert _ids(check_block_enforcement(None)) == ["stapel_chat.E018"]

    def test_an_empty_block_function_is_reported_as_unreachable(self, settings):
        settings.STAPEL_CHAT = {"BLOCK_ENFORCEMENT": "required", "BLOCK_FUNCTION": ""}
        assert _ids(check_block_enforcement(None)) == ["stapel_chat.E017"]


class TestSubjectChecks:
    def test_the_empty_registry_is_silent(self):
        """Silence is the normal state of a generic chat."""
        assert check_subject_types(None) == []

    def test_a_type_without_a_card_function_is_an_error(self, settings):
        """Registered through settings, which does not go through
        `register_subject_type`'s guard — so the check is the backstop."""
        settings.STAPEL_CHAT = {"SUBJECT_TYPES": {"listing": {"label": "x"}}}
        assert _ids(check_subject_types(None)) == ["stapel_chat.E020"]

    def test_a_type_that_is_not_a_dict_is_an_error(self, settings):
        settings.STAPEL_CHAT = {"SUBJECT_TYPES": {"listing": "classified.cards"}}
        assert _ids(check_subject_types(None)) == ["stapel_chat.E020"]

    def test_an_unreachable_card_function_warns_rather_than_refusing_to_boot(self):
        """A Warning, not an Error: over a bus transport nothing in this
        process can prove a remote provider is up, and refusing to boot on
        that would make chat undeployable ahead of its provider."""
        register_subject_type("listing", {"card_function": "classified.subject_cards"})
        assert _ids(check_subject_types(None)) == ["stapel_chat.W005"]

    def test_a_reachable_card_function_is_silent(self):
        @function("classified.subject_cards")
        def _cards(payload):
            return {"cards": {}}

        register_subject_type("listing", {"card_function": "classified.subject_cards"})
        assert check_subject_types(None) == []
