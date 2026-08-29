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
  there (found on a client NATS deploy). There the check downgrades E005
  to stapel_chat.W002: it cannot verify, so it says so instead of guessing.
- CHAT_KINDS not a subset of {direct, group, support} -> E (an unknown kind
  would be un-creatable and confuse the capability report).
- MAX_BODY_LENGTH not a positive int -> E (would reject or admit bodies
  nonsensically).

Since 0.3.0 the realtime block (E010-E014) applies the same policy to the thing
a chat actually is. Each of those checks names one way a deployment used to be
able to serve this module and still ship a product that refreshed on a timer,
with nothing anywhere saying so. They are ERRORs and there is no setting that
downgrades them: a silent polling fallback is the defect, so the configuration
that produces one must not boot. E015/E016 validate the two open registries at
boot rather than at send time.
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


@checks.register(checks.Tags.compatibility)
def check_presence_windows(app_configs, **kwargs):
    """The write throttle must fit inside the lease.

    Presence renews its lease on the same write the throttle rations. Set the
    throttle at or above the TTL and a perfectly healthy socket lets its own
    lease lapse between two permitted writes — the peer then watches somebody
    who is sitting right there blink offline on a timer. That is the same
    class of defect as the "На связи" this surface replaced: a confident
    statement about another person derived from the wrong fact. It is a
    boot failure rather than a runtime surprise because the arithmetic is
    knowable at check time.
    """
    from .conf import chat_settings

    issues = []
    ttl = chat_settings.PRESENCE_TTL_S
    throttle = chat_settings.PRESENCE_WRITE_THROTTLE_S
    fanout = chat_settings.PRESENCE_FANOUT_LIMIT
    for name, value, floor in (
        ("PRESENCE_TTL_S", ttl, 1),
        ("PRESENCE_WRITE_THROTTLE_S", throttle, 0),
        ("PRESENCE_FANOUT_LIMIT", fanout, 0),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < floor:
            issues.append(
                checks.Error(
                    f"STAPEL_CHAT['{name}'] must be an integer >= {floor}.",
                    id="stapel_chat.E021",
                )
            )
    if issues:
        return issues
    if throttle >= ttl:
        return [
            checks.Error(
                f"STAPEL_CHAT['PRESENCE_WRITE_THROTTLE_S'] ({throttle}) is not "
                f"below ['PRESENCE_TTL_S'] ({ttl}): a live socket would let "
                "its own presence lease expire between two permitted writes, "
                "and the other party would see it go offline while it is up.",
                hint="Keep the throttle well under the TTL (30 under 90 by default).",
                id="stapel_chat.E021",
            )
        ]
    return []


# ── Realtime: the socket is the product, so its absence is an error ──────
#
# 0.3.0's whole reason for existing. A deployment used to be able to install
# this module, serve the REST surface, and ship a chat that refreshed on a
# timer — with nothing anywhere saying so. Every check below turns one silent
# way of arriving at that outcome into a boot failure.
#
# There is deliberately no setting that switches them off. A knob would
# reproduce the defect: the product that shipped "updates every few seconds"
# got there without anyone choosing it, and an opt-out is how the next one
# would too.


def _cookie_auth_name():
    """The JWT cookie this deployment authenticates HTTP with, if it does.

    Returns the cookie name when the DRF stack includes the cookie
    authentication class, else ``None``. Cookie mode is the default for a
    browser SPA (that is what ``JWTCookieAuthentication`` is for), and it is
    the exact configuration that produced the polling fallback: the HTTP path
    reads a cookie, the WebSocket path did not.
    """
    from django.conf import settings

    classes = (getattr(settings, "REST_FRAMEWORK", {}) or {}).get(
        "DEFAULT_AUTHENTICATION_CLASSES"
    ) or []
    if not any("JWTCookie" in str(entry) for entry in classes):
        return None
    return getattr(settings, "JWT_COOKIE_NAME", "stapel_jwt")


def _ws_middleware_reads_cookies(cookie_name: str) -> bool:
    """Ask the Channels JWT middleware, functionally, whether it can read a
    cookie off a handshake.

    A probe rather than a version pin: the question is about behaviour, and a
    behavioural question deserves a behavioural answer that stays true however
    the core reorganizes its extractor.
    """
    try:
        from stapel_core.django.jwt.channels import _extract_token
    except Exception:  # pragma: no cover - the core moved the extractor
        return True  # cannot ask -> do not assert
    scope = {
        "headers": [(b"cookie", f"{cookie_name}=probe.token.value".encode())],
        "query_string": b"",
        "subprotocols": [],
    }
    try:
        return _extract_token(scope) == "probe.token.value"
    except Exception:  # pragma: no cover - a probe must never break boot
        return True


@checks.register(checks.Tags.compatibility)
def check_realtime_substrate(app_configs, **kwargs):
    """stapel_realtime must be installed — chat's sockets are built on it."""
    from django.apps import apps

    if apps.is_installed("stapel_realtime"):
        return []
    return [
        checks.Error(
            "stapel-chat serves its messages over WebSocket and builds both "
            "sockets on stapel-realtime, which is not in INSTALLED_APPS. "
            "Without it the consumers cannot be resolved, the signal "
            "transport is never registered, and the realtime system checks "
            "that would have told you all this never run.",
            hint="Add 'stapel_realtime' to INSTALLED_APPS and install the "
                 "extra: pip install 'stapel-chat[realtime]'.",
            id="stapel_chat.E010",
        )
    ]


@checks.register(checks.Tags.compatibility)
def check_channel_layer(app_configs, **kwargs):
    """No channel layer == a socket that connects and then never says anything."""
    from django.conf import settings

    layers = getattr(settings, "CHANNEL_LAYERS", None) or {}
    if layers.get("default"):
        return []
    return [
        checks.Error(
            "CHANNEL_LAYERS has no 'default' backend, so chat's fan-out is a "
            "no-op: sockets connect, replay history once, and then go silent "
            "while every new message arrives only to whoever is in the same "
            "process. This is the configuration that makes a product poll.",
            hint="Configure channels_redis.core.RedisChannelLayer in "
                 "production; channels.layers.InMemoryChannelLayer is enough "
                 "for a single-process test run.",
            id="stapel_chat.E011",
        )
    ]


@checks.register(checks.Tags.security)
def check_websocket_credential_channel(app_configs, **kwargs):
    """The defect that shipped: HTTP authenticates by cookie, WS could not.

    A browser cannot set an ``Authorization`` header on ``new WebSocket()``.
    If the deployment's HTTP stack authenticates by cookie and the Channels
    middleware has no cookie branch, then every handshake a browser makes
    closes 4401 — and a client that reads 4401 as a permanent refusal stops
    retrying and falls back to whatever it has left, which is a timer.
    """
    cookie_name = _cookie_auth_name()
    if cookie_name is None:
        return []
    if _ws_middleware_reads_cookies(cookie_name):
        return []
    return [
        checks.Error(
            f"This deployment authenticates HTTP with the {cookie_name!r} "
            "cookie, but the Channels JWT middleware only reads a token from "
            "the Authorization header, the Sec-WebSocket-Protocol subprotocol "
            "or ?token=. A browser can set none of those on new WebSocket(), "
            "so every chat handshake from a browser will close 4401 and the "
            "client will fall back to polling.",
            hint="Upgrade stapel-core to a version whose Channels middleware "
                 "reads the JWT cookie, or hand the frontend a token to send "
                 "as a subprotocol. Pair either with a non-empty "
                 "STAPEL_REALTIME['ALLOWED_ORIGINS'] — see stapel_chat.E014.",
            id="stapel_chat.E012",
        )
    ]


@checks.register(checks.Tags.compatibility)
def check_signal_transport(app_configs, **kwargs):
    """Typing indicators and receipts ride the Signal seam, not the journal."""
    try:
        from stapel_core.comm.signals import signal_transport
    except Exception:  # pragma: no cover - core moved the resolver
        return []
    if signal_transport() is not None:
        return []
    return [
        checks.Error(
            "STAPEL_COMM['SIGNAL_TRANSPORT'] resolves to no transport, so "
            "every ephemeral frame chat emits — typing and activity states, "
            "read and delivery receipts, the inbox stream that keeps a "
            "conversation list live — is dropped silently. The journal would "
            "still work, which is exactly why this is worth failing on: the "
            "result looks like a working chat with dead ticks and a "
            "conversation list that only moves when you reload it.",
            hint="Set STAPEL_COMM['SIGNAL_TRANSPORT'] = 'channels' "
                 "(stapel-realtime registers it from its AppConfig.ready()).",
            id="stapel_chat.E013",
        )
    ]


@checks.register(checks.Tags.security)
def check_origin_guard(app_configs, **kwargs):
    """A cookie is ambient authority; an unguarded socket accepts it from
    anywhere.

    stapel-realtime already warns (``realtime.W002``) that an empty
    ``ALLOWED_ORIGINS`` disables the origin guard. Where the socket
    authenticates by cookie that is not a warning — the browser attaches the
    credential to a handshake from *any* page, so an unguarded socket is
    cross-site WebSocket hijacking of a live conversation.
    """
    if _cookie_auth_name() is None:
        return []
    try:
        from stapel_realtime.conf import realtime_settings

        origins = list(realtime_settings.ALLOWED_ORIGINS or [])
    except Exception:  # pragma: no cover - reported by E010
        return []
    if origins:
        return []
    return [
        checks.Error(
            "This deployment authenticates the chat socket by cookie and "
            "STAPEL_REALTIME['ALLOWED_ORIGINS'] is empty, so the origin guard "
            "is off. A browser attaches the cookie to a WebSocket handshake "
            "started by any page on the internet: an unguarded socket lets an "
            "attacker's page read and post in the victim's conversations.",
            hint="List this deployment's origins WITH their ports, e.g. "
                 "STAPEL_REALTIME['ALLOWED_ORIGINS'] = "
                 "['https://app.example.com', 'http://localhost:5173'].",
            id="stapel_chat.E014",
        )
    ]


@checks.register(checks.Tags.compatibility)
def check_attachment_registry(app_configs, **kwargs):
    """A registry entry that is not a spec would fail at send time, not boot."""
    from .attachments import get_attachment_types

    issues = []
    try:
        types = get_attachment_types()
    except Exception as exc:
        return [
            checks.Error(
                f"STAPEL_CHAT['ATTACHMENT_TYPES'] could not be merged: {exc}",
                id="stapel_chat.E015",
            )
        ]
    for name, spec in types.items():
        if not isinstance(spec, dict):
            issues.append(
                checks.Error(
                    f"Attachment type {name!r} must map to a dict "
                    f"(got {type(spec).__name__}); use None to remove a builtin.",
                    id="stapel_chat.E015",
                )
            )
            continue
        fields = spec.get("fields", ())
        if not isinstance(fields, (list, tuple)):
            issues.append(
                checks.Error(
                    f"Attachment type {name!r} declares a non-sequence 'fields'.",
                    id="stapel_chat.E015",
                )
            )
    return issues


@checks.register(checks.Tags.compatibility)
def check_activity_registry(app_configs, **kwargs):
    """Same, for the activity states."""
    from .activity import get_activity_states

    issues = []
    try:
        states = get_activity_states()
    except Exception as exc:
        return [
            checks.Error(
                f"STAPEL_CHAT['ACTIVITY_STATES'] could not be merged: {exc}",
                id="stapel_chat.E016",
            )
        ]
    for name, spec in states.items():
        if not isinstance(spec, dict):
            issues.append(
                checks.Error(
                    f"Activity state {name!r} must map to a dict "
                    f"(got {type(spec).__name__}); use None to remove a builtin.",
                    id="stapel_chat.E016",
                )
            )
            continue
        ttl = spec.get("ttl_s", 0)
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0:
            issues.append(
                checks.Error(
                    f"Activity state {name!r} declares ttl_s={ttl!r}; it must "
                    "be a non-negative integer number of seconds.",
                    id="stapel_chat.E016",
                )
            )
    return issues


# ── Subjects and blocks (0.6.0) ──────────────────────────────────────────


@checks.register(checks.Tags.compatibility)
def check_subject_types(app_configs, **kwargs):
    """A subject type nobody can render is a string in a database.

    The registry ships empty, so silence here is the normal state of a generic
    chat. What is refused is a declared type whose ``card_function`` is
    missing or names something that will never answer: without this the defect
    surfaces as a blank header the first time a real user opens a real thread.
    """
    from stapel_core.comm import function_unreachable_reason

    from .subjects import get_subject_types

    try:
        types = get_subject_types()
    except Exception as exc:
        return [
            checks.Error(
                f"STAPEL_CHAT['SUBJECT_TYPES'] could not be merged: {exc}",
                id="stapel_chat.E020",
            )
        ]

    issues = []
    for name, policy in types.items():
        if not isinstance(policy, dict):
            issues.append(
                checks.Error(
                    f"Subject type {name!r} must map to a dict "
                    f"(got {type(policy).__name__}); use None to remove one.",
                    id="stapel_chat.E020",
                )
            )
            continue
        card_function = (policy.get("card_function") or "").strip()
        if not card_function:
            issues.append(
                checks.Error(
                    f"Subject type {name!r} declares no 'card_function'. A "
                    "conversation about it would render a header nothing can "
                    "fill — the exact 'unclear about what' this surface "
                    "exists to close.",
                    hint="Name the batched comm Function that turns keys into "
                         "cards, e.g. {'card_function': "
                         "'classified.subject_cards'}.",
                    id="stapel_chat.E020",
                )
            )
            continue
        reason = function_unreachable_reason(card_function)
        if reason:
            # A Warning, not an Error: over a bus transport nothing here can
            # prove a remote provider is up, and refusing to boot on that
            # would make chat undeployable ahead of its provider.
            issues.append(
                checks.Warning(
                    f"Subject type {name!r} names card_function "
                    f"{card_function!r}, which is not reachable from this "
                    f"process: {reason}. Conversations about this subject "
                    "will render with meta_status='partial' and "
                    "meta_reason='card_function_unreachable' until it is.",
                    hint="Expected if the provider is a separate service "
                         "reached over the bus. If it is meant to be "
                         "in-process, its module is not imported here.",
                    id="stapel_chat.W005",
                )
            )
    return issues


@checks.register(checks.Tags.security)
def check_block_enforcement(app_configs, **kwargs):
    """Say out loud, at every boot, whether blocks are enforced here.

    A block that stops nothing is the kind of thing a deployment discovers
    from a user complaint. All three states are announced — including the
    deliberate 'off', because a decision on the record is not a defect but an
    undeclared one is.
    """
    from .blocks import (
        ENFORCEMENT_MODES,
        ENFORCEMENT_OFF,
        ENFORCEMENT_REQUIRED,
        enforcement_mode,
        provider_unreachable_reason,
    )
    from .conf import chat_settings

    raw = str(chat_settings.BLOCK_ENFORCEMENT or "").strip().lower()
    if raw and raw not in ENFORCEMENT_MODES:
        return [
            checks.Error(
                f"STAPEL_CHAT['BLOCK_ENFORCEMENT'] is {raw!r}; it must be one "
                f"of {list(ENFORCEMENT_MODES)}. An unrecognized value is read "
                "as 'auto' at runtime, which is not a thing to leave implicit "
                "for a security control.",
                id="stapel_chat.E018",
            )
        ]

    mode = enforcement_mode()
    if mode == ENFORCEMENT_OFF:
        return [
            checks.Warning(
                "STAPEL_CHAT['BLOCK_ENFORCEMENT'] is 'off': in this deployment "
                "a blocked user may open a direct thread with whoever blocked "
                "them, and may send into one. Both doors are unlocked — "
                "since 0.6.1 this module holds them both, so nothing else in "
                "the fleet is covering for this setting.",
                hint="Set it to 'auto' (enforce when a provider is reachable) "
                     "or 'required' (refuse to run without one).",
                id="stapel_chat.W004",
            )
        ]

    reason = provider_unreachable_reason()
    if not reason:
        return []
    name = (chat_settings.BLOCK_FUNCTION or "").strip()
    if mode == ENFORCEMENT_REQUIRED:
        return [
            checks.Error(
                f"STAPEL_CHAT['BLOCK_ENFORCEMENT'] is 'required' and the "
                f"block provider {name!r} is not reachable: {reason}. Opening "
                "a NEW direct thread and sending into one will both answer "
                "503 until it is — which is the correct behaviour for "
                "'required' and a broken deployment either way. Returning a "
                "thread that already exists is unaffected: that is a read of "
                "history, and it asks the provider nothing.",
                hint="stapel-profiles >= 0.16.0 is the first release serving "
                     "'profiles.relationships': install/mount it here, or "
                     "deploy that service on that floor FIRST and configure "
                     "its function route; alternatively repoint "
                     "STAPEL_CHAT['BLOCK_FUNCTION'], or drop to 'auto'. Over "
                     "a bus transport this check CANNOT see a stale remote "
                     "provider — confirm the floor is DEPLOYED, not merely "
                     "pinned; a stale one is refused at the first call.",
                id="stapel_chat.E017",
            )
        ]
    return [
        checks.Warning(
            f"Block enforcement is 'auto' and the block provider {name!r} is "
            f"not reachable from this process: {reason}. Blocks are NOT "
            "enforced in this deployment — neither on the send path nor when "
            "a new direct thread is opened.",
            hint="Expected while the provider is a separate service reached "
                 "over the bus, or before it ships. Set 'required' to make "
                 "its absence a boot failure instead of an advisory.",
            id="stapel_chat.W003",
        )
    ]
