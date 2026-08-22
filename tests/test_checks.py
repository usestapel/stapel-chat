"""System checks for stapel-chat configuration."""
import pytest

from stapel_chat.checks import (
    check_chat_kinds,
    check_max_body_length,
    check_scope_provider,
)
from stapel_chat.scope import ScopeProvider


class HostScopeProvider(ScopeProvider):
    """What the checks want a production host to write."""

    def resolve(self, request):
        return "ws-1"

    def filter(self, queryset, request):
        return queryset.filter(scope_key="ws-1")

    def can_operate(self, request, conversation=None):
        return False


def test_default_config_is_clean():
    assert check_chat_kinds(None) == []
    assert check_max_body_length(None) == []


def test_the_shipped_provider_warns_in_a_standalone_deployment():
    """No longer silent: importability and type said nothing about a
    single-scope provider carrying a whole deployment."""
    msgs = check_scope_provider(None)
    assert [m.id for m in msgs] == ["stapel_chat.W001"]


def test_the_shipped_provider_is_an_error_where_workspaces_can_answer():
    """The finding the old check could not make: this deployment knows what a
    mandate is, and the shipped provider cannot name a tenant. inprocess is a
    decidable transport (the registry proves it), so E005 fires here."""
    from stapel_core.comm import function_registry
    from stapel_core.django.mandate import MANDATE_FUNCTION, MANDATE_RESULT_KEY

    function_registry.register(
        MANDATE_FUNCTION, lambda payload: {MANDATE_RESULT_KEY: True}
    )
    try:
        msgs = check_scope_provider(None)
    finally:
        function_registry._providers.pop(MANDATE_FUNCTION, None)
    assert [m.id for m in msgs] == ["stapel_chat.E005"]


def test_a_real_swap_is_silent(settings):
    settings.STAPEL_CHAT = {"SCOPE_PROVIDER": "tests.test_checks.HostScopeProvider"}
    assert check_scope_provider(None) == []


def test_a_real_swap_is_silent_under_nats(settings):
    """A genuine override is silent regardless of transport: the check never
    gets far enough to ask about workspaces reachability."""
    settings.STAPEL_CHAT = {"SCOPE_PROVIDER": "tests.test_checks.HostScopeProvider"}
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "nats"}
    assert check_scope_provider(None) == []


def test_the_shipped_provider_is_a_warning_under_nats_when_absent(settings):
    """stapel_chat.E005 regression (client NATS fleet): under nats,
    comm.function_unreachable_reason returns None unconditionally — "not
    provably unreachable" is not "reachable". The check must not read a
    genuinely standalone nats deployment as having workspaces."""
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "nats"}
    msgs = check_scope_provider(None)
    assert [m.id for m in msgs] == ["stapel_chat.W002"]


def test_the_shipped_provider_is_a_warning_under_nats_even_if_registered_locally(settings):
    """A local registration is invisible to the nats transport (the bus
    dispatches by subject, not the process-local registry) — registering it
    here must not change the verdict from the unknowable-over-the-bus case."""
    from stapel_core.comm import function_registry
    from stapel_core.django.mandate import MANDATE_FUNCTION, MANDATE_RESULT_KEY

    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "nats"}
    function_registry.register(
        MANDATE_FUNCTION, lambda payload: {MANDATE_RESULT_KEY: True}
    )
    try:
        msgs = check_scope_provider(None)
    finally:
        function_registry._providers.pop(MANDATE_FUNCTION, None)
    assert [m.id for m in msgs] == ["stapel_chat.W002"]


def test_unverifiable_over_the_bus_only_touches_e005():
    """The downgrade helper is a targeted swap, not a blanket rewrite: any
    other issue (e.g. W001, or an issue this module does not know about
    today) passes through untouched."""
    from django.core import checks as django_checks

    from stapel_chat.checks import _as_unverifiable_over_the_bus

    other = django_checks.Warning("unrelated", id="stapel_chat.W001")
    assert _as_unverifiable_over_the_bus(other) is other


def test_bad_scope_provider_is_error(settings):
    settings.STAPEL_CHAT = {"SCOPE_PROVIDER": "stapel_chat.does.not.Exist"}
    errors = check_scope_provider(None)
    assert errors and errors[0].id == "stapel_chat.E001"


def test_unknown_kind_is_error(settings):
    settings.STAPEL_CHAT = {"CHAT_KINDS": ["direct", "telepathy"]}
    errors = check_chat_kinds(None)
    assert errors and errors[0].id == "stapel_chat.E003"


def test_empty_kinds_is_error(settings):
    settings.STAPEL_CHAT = {"CHAT_KINDS": []}
    assert check_chat_kinds(None)[0].id == "stapel_chat.E003"


@pytest.mark.parametrize("bad", [0, -1, "lots", True])
def test_bad_max_body_length_is_error(settings, bad):
    settings.STAPEL_CHAT = {"MAX_BODY_LENGTH": bad}
    errors = check_max_body_length(None)
    assert errors and errors[0].id == "stapel_chat.E004"
