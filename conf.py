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

**There is no key that turns realtime off.** Since 0.3.0 the socket is the
canonical path and a deployment that cannot serve one fails ``manage.py
check`` (``stapel_chat.E010``-``E014``) instead of degrading into a poll. A
knob here would be the defect: the product that shipped "updates every few
seconds" got there without anybody choosing it.

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
- ``ATTACHMENT_TYPES`` — the OPEN attachment-type registry (merge over
  builtins, ``None`` removes). Stickers are the named next type; adding one is
  a settings line, not a release here.
- ``ACTIVITY_STATES`` — the OPEN activity registry ("typing", "recording
  audio", …), same merge semantics. "Choosing a sticker" is a settings line.
- ``EDIT_WINDOW_S`` — how long after posting an author may still edit their
  own message. ``0`` means forever.
- ``MODERATION_TARGET_TYPE`` — the stapel-moderation target type registered
  for chat messages, when that module is installed and no host has declared
  the type itself. ``""`` registers nothing.
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
    # OPEN attachment-type registry (attachments.py). Merged OVER the builtins
    # {image, gif, video, audio, file} — the SAME names as stapel-cdn's media
    # kinds; a value of None removes a builtin. The
    # entry declares `fields` — what a UI may expect populated for the type —
    # and `media` — whether the ref resolves to a CDN asset worth describing.
    "ATTACHMENT_TYPES": {},
    # OPEN activity-state registry (activity.py). Merged OVER the builtins
    # {idle, typing, recording_audio, sending_video, uploading_file}; None
    # removes one. Each entry carries a `ttl_s` expiry hint for the client.
    "ACTIVITY_STATES": {},
    # Where an attachment's render metadata comes from: "cdn" asks
    # `cdn.describe` by comm and merges its answer over the client's (the
    # default — the CDN owns aspect/thumbnail/waveform); "client" trusts the
    # sender and makes no call.
    "ATTACHMENT_METADATA": "cdn",
    # Most attachments one message may carry.
    "MAX_ATTACHMENTS": 10,
    # Ceiling on an inline base64 preview in bytes. Matches stapel-cdn's own
    # MICRO_PREVIEW_MAX_BYTES default, measured the same way (on the finished
    # data: URI, base64 expansion included) — a second, larger number here
    # would accept what the authority already refused. These are untrusted
    # bytes riding inside every message frame on their way to other people's
    # screens, and they multiply: MAX_ATTACHMENTS x this is the per-message
    # payload floor a client pays for previews.
    "MAX_PREVIEW_B64_BYTES": 4096,
    # Seconds an author may still edit their own message. 0 = no window.
    "EDIT_WINDOW_S": 0,
    # The stapel-moderation target type this module registers for its own
    # messages when stapel-moderation is installed AND no host has declared
    # that type already (moderation.py). "" registers nothing: a deployment
    # that wants complaints about messages handled somewhere else, or not at
    # all, says so here rather than by uninstalling a dependency.
    "MODERATION_TARGET_TYPE": "chat_message",
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
