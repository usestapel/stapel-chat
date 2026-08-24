"""Single-module Django settings for stapel-chat's harnesses.

Single source of truth for the ``settings.configure(...)`` block shared by:

  - the pytest suite (``conftest.py``) — mounts chat on its *bare* test urlconf
    (``stapel_chat.tests.urls`` -> ``chat/`` -> the module's own ``api/*``); and
  - the contract-emission harness (``_codegen.py`` / ``make contract``) — mounts
    chat on its *canonical* public API prefix (``stapel_chat.codegen_urls`` ->
    ``chat/`` -> the same ``api/*`` paths) and enables drf-spectacular, so the
    emitted ``schema.json`` / ``flows.json`` paths are byte-identical to what a
    host mounting this module would serve (contract-pipeline.md §2).

Keeping one copy here means the harness and the tests can never drift in their
``INSTALLED_APPS`` / comm config. Copied from stapel-calendar's adaptation of
the stapel-auth etalon; tailored to this module (no gdpr/social_django/JWT/
Twilio — chat carries none of that, but it needs the in-process comm bus +
schema validation the tests rely on).
"""
from __future__ import annotations


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_chat.tests.urls",
    contract: bool = False,
    extra_apps: tuple = (),
) -> dict:
    """Return the ``settings.configure(**kwargs)`` for a single-module chat
    instance. ``contract=True`` swaps in the production ``REST_FRAMEWORK`` (the
    canonical stapel-core config) so the emitted schema is byte-identical to a
    real host's.

    ``extra_apps`` is for the TEST harness only, and today holds exactly one
    thing: ``stapel_moderation``, so the moderation seam can be exercised
    against the real queue instead of a mock of it. The contract harness never
    passes it — a co-mounted module would put its error keys in this module's
    emitted catalogue."""
    if contract:
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        rest_framework = None

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.admin",
            "django.contrib.messages",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "rest_framework",
            "drf_spectacular",
            # The WebSocket substrate. In INSTALLED_APPS because it carries
            # system checks and registers the "channels" signal transport from
            # its AppConfig.ready() — the same two lines a host writes.
            "stapel_realtime",
            "stapel_chat",
            *extra_apps,
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        # Synchronous in-process comm with schema validation ON, so the
        # committed contracts in schemas/ are enforced by the tests.
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        STAPEL_COMM={
            "OUTBOX_ENABLED": False,
            "ACTION_TRANSPORT": "inprocess",
            "VALIDATE_SCHEMAS": True,
            # Signals (typing, receipts, the inbox stream) are delivered by
            # stapel-realtime's Channels transport. Leaving this at the "none"
            # default is what stapel_chat.E013 refuses.
            "SIGNAL_TRANSPORT": "channels",
        },
        # In-memory Channels layer so the realtime consumer tests can exercise
        # group fan-out without a broker.
        CHANNEL_LAYERS={
            "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
        },
        STAPEL_REALTIME={
            # Exact origins, with their ports — an entry without the port is
            # an allowlist that silently never matches (realtime.E003).
            "ALLOWED_ORIGINS": ["http://testserver"],
        },
        MIGRATION_MODULES={
            "users": None,
            "chat": None,
        },
    )
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


# The multi-module common path prefix drf-spectacular auto-detects in a
# multi-module aggregate. Forced on the drf-spectacular settings singleton by
# the harness so a single-module instance derives the same style of
# operationIds. Uniform across all pair-backends.
CODEGEN_SCHEMA_PATH_PREFIX = "/"
