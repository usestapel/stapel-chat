"""Django system checks for stapel-chat configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the service
cannot run with; W-level for entries that only degrade lazily.

- SCOPE_PROVIDER unimportable / not a ScopeProvider -> E (create & list cannot
  resolve/filter scope).
- CHAT_KINDS not a subset of {direct, group, support} -> E (an unknown kind
  would be un-creatable and confuse the capability report).
- MAX_BODY_LENGTH not a positive int -> E (would reject or admit bodies
  nonsensically).
"""
from django.core import checks

_VALID_KINDS = {"direct", "group", "support"}


@checks.register(checks.Tags.compatibility)
def check_scope_provider(app_configs, **kwargs):
    from .conf import chat_settings
    from .scope import ScopeProvider

    try:
        provider = chat_settings.SCOPE_PROVIDER
    except Exception as exc:
        return [
            checks.Error(
                f"STAPEL_CHAT['SCOPE_PROVIDER'] could not be imported: {exc}",
                id="stapel_chat.E001",
            )
        ]
    target = provider if isinstance(provider, type) else type(provider)
    if not issubclass(target, ScopeProvider):
        return [
            checks.Error(
                "STAPEL_CHAT['SCOPE_PROVIDER'] must be a ScopeProvider subclass",
                id="stapel_chat.E002",
            )
        ]
    return []


@checks.register(checks.Tags.compatibility)
def check_chat_kinds(app_configs, **kwargs):
    from .conf import chat_settings

    kinds = chat_settings.CHAT_KINDS
    if not isinstance(kinds, (list, tuple)) or not kinds or (
        set(kinds) - _VALID_KINDS
    ):
        return [
            checks.Error(
                "STAPEL_CHAT['CHAT_KINDS'] must be a non-empty subset of "
                "{direct, group, support}.",
                id="stapel_chat.E003",
            )
        ]
    return []


@checks.register(checks.Tags.compatibility)
def check_max_body_length(app_configs, **kwargs):
    from .conf import chat_settings

    value = chat_settings.MAX_BODY_LENGTH
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return [
            checks.Error(
                "STAPEL_CHAT['MAX_BODY_LENGTH'] must be a positive integer.",
                id="stapel_chat.E004",
            )
        ]
    return []
