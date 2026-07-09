"""System checks for stapel-chat configuration."""
import pytest

from stapel_chat.checks import (
    check_chat_kinds,
    check_max_body_length,
    check_scope_provider,
)


def test_default_config_is_clean():
    assert check_scope_provider(None) == []
    assert check_chat_kinds(None) == []
    assert check_max_body_length(None) == []


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
