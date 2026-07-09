"""Settings namespace for stapel-chat.

All configuration is read through ``chat_settings`` (lazily, at call time) —
never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_CHAT`` dict -> flat Django setting
of the same name -> environment variable -> default below.

Dotted-path keys listed in ``import_strings`` are resolved with
``import_string`` — the fork-free escape hatch for swappable behavior.

The one documented extension seam (see MODULE.md):

- ``SCOPE_PROVIDER`` — resolves/filters the opaque ``scope_key`` from the
  request (a host may supply e.g. ``workspace_id``). The library is
  scope-agnostic; the default is a no-op single global scope.

CTO-facing config axes (capability-config.md §16):

- ``CHAT_KINDS`` — which conversation kinds this deployment offers
  (``direct`` / ``group`` / ``support``). Drop ``support`` to run a pure
  peer-to-peer chat with no operator queue; drop ``group`` to allow only 1:1
  direct threads. Creating (or, for support, operating on) a kind that is not
  enabled is refused. The default enables all three.
- ``ATTACHMENTS`` — whether messages may carry attachment keys at all. When
  off, a message with a non-empty ``attachments`` list is rejected; the module
  never stores files itself, only opaque keys that point at the host's CDN.
- ``MAX_BODY_LENGTH`` — hard cap on a text message body (characters). A longer
  body is rejected before it reaches the database.
"""
from stapel_core.conf import AppSettings

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
    # Which conversation kinds are offered. A "list" axis: the enabled subset
    # of {direct, group, support}. Removing "support" turns off the whole
    # operator queue/assignment surface; removing "group" leaves only 1:1
    # direct threads. An unknown kind in the request is refused.
    "CHAT_KINDS": ["direct", "group", "support"],
    # Whether messages may carry attachment keys. A "bool" behavior axis: when
    # False, any message with a non-empty attachments list is rejected. Files
    # live in the host's CDN/storage; the module persists only opaque keys.
    "ATTACHMENTS": True,
    # Hard cap on a text body in characters (an "int" tuning axis). Bodies over
    # this length are rejected up front.
    "MAX_BODY_LENGTH": 4000,
    # Dotted path to a ScopeProvider — resolves the opaque scope_key from a
    # request and filters querysets by it. The default is a no-op (single
    # global scope); a host may return e.g. the active workspace_id.
    "SCOPE_PROVIDER": "stapel_chat.scope.DefaultScopeProvider",
}

chat_settings = AppSettings(
    "STAPEL_CHAT",
    defaults=DEFAULTS,
    import_strings=("SCOPE_PROVIDER",),
)

__all__ = ["chat_settings", "DEFAULTS"]
