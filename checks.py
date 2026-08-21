"""Django system checks for stapel-chat configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the service
cannot run with; W-level for entries that only degrade lazily.

- SCOPE_PROVIDER unimportable / not a ScopeProvider -> E (create & list cannot
  resolve/filter scope).
- SCOPE_PROVIDER still the shipped single-scope default while this deployment
  has workspaces -> E; standalone -> W. Importability and type were the only
  things ever validated here, so nothing said a multi-tenant host was running
  the default that puts every tenant in one scope.
- stapel_chat.E005 only fires where "does this deployment have workspaces" is
  provable at boot (inprocess/http FUNCTION_TRANSPORT). Under a bus transport
  (nats, or a dotted custom transport) `comm.function_unreachable_reason`
  returns None unconditionally, by its own docstring, because nothing at boot
  can prove a bus provider is up — reading that as "workspaces present" is a
  false E005 on every such fleet regardless of whether workspaces is actually
  there (found on the darom.ai NATS deploy). There the check downgrades E005
  to stapel_chat.W002: it cannot verify, so it says so instead of guessing.
- CHAT_KINDS not a subset of {direct, group, support} -> E (an unknown kind
  would be un-creatable and confuse the capability report).
- MAX_BODY_LENGTH not a positive int -> E (would reject or admit bodies
  nonsensically).
"""
from django.core import checks
from stapel_core.django.scope import check_shipped_scope_provider

_VALID_KINDS = {"direct", "group", "support"}

#: FUNCTION_TRANSPORT values `comm.function_unreachable_reason` can actually
#: decide for a name: inprocess settles it against the local registry, http
#: against FUNCTION_ROUTES. nats and a dotted custom transport are the other
#: two branches in that function, and both return None unconditionally — "not
#: provably unreachable" there is not "reachable", it is "unknowable here".
_DECIDABLE_FUNCTION_TRANSPORTS = {"inprocess", "http"}


def _mandate_reachability_is_decidable() -> bool:
    """Can this process prove, at boot, whether workspaces.check_mandate is
    reachable? Mirrors the transport branches of
    ``stapel_core.comm.function_unreachable_reason`` without calling it,
    because that function's contract is "None means not provably
    unreachable", and stapel_chat.E005 needs the narrower "None means
    provably reachable" that only the inprocess/http branches supply."""
    from stapel_core.comm import comm_setting

    transport = str(comm_setting("FUNCTION_TRANSPORT", "inprocess") or "")
    return transport in _DECIDABLE_FUNCTION_TRANSPORTS


def _as_unverifiable_over_the_bus(issue):
    """Downgrade stapel_chat.E005 to a W-level advisory when the deployment's
    comm transport cannot prove workspaces reachability at boot. Any other
    issue (W001, or none) passes through unchanged."""
    if issue.id != "stapel_chat.E005":
        return issue
    return checks.Warning(
        "STAPEL_CHAT['SCOPE_PROVIDER'] is the shipped single-scope provider, "
        "and this deployment's comm FUNCTION_TRANSPORT cannot prove at boot "
        "whether workspaces is reachable here (bus transports never can — "
        "nothing here can, or should, verify a remote provider is up before "
        "the first call). If workspaces IS reachable, the shipped provider "
        "has the same live tenancy hole stapel_chat.E005 names, and it stays "
        "silent rather than surfacing here. If it is not, this deployment is "
        "genuinely standalone and the shipped provider is correct as shipped.",
        hint="Point STAPEL_CHAT['SCOPE_PROVIDER'] at a workspace-aware "
             "provider if this deployment has workspaces — the check cannot "
             "tell over this transport. A mandate lookup that is truly "
             "unwired still fails loud at runtime (MandateUnavailable, 503), "
             "never silently admits.",
        id="stapel_chat.W002",
    )


@checks.register(checks.Tags.compatibility)
def check_scope_provider(app_configs, **kwargs):
    from .conf import chat_settings
    from .scope import DefaultScopeProvider, ScopeProvider

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
    # Importable and correctly typed says nothing about whether the shipped
    # single-scope default is still carrying a multi-tenant deployment.
    issues = check_shipped_scope_provider(
        setting="STAPEL_CHAT['SCOPE_PROVIDER']",
        provider=provider,
        shipped_cls=DefaultScopeProvider,
        error_id="stapel_chat.E005",
        warning_id="stapel_chat.W001",
        isolates="conversation",
    )
    if _mandate_reachability_is_decidable():
        return issues
    # Over a bus transport "not provably unreachable" is not "reachable" —
    # E005 asserted a fact this process cannot prove. Say so instead.
    return [_as_unverifiable_over_the_bus(issue) for issue in issues]


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
