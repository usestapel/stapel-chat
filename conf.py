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
- ``PRESENCE_TTL_S`` / ``PRESENCE_WRITE_THROTTLE_S`` /
  ``PRESENCE_FANOUT_LIMIT`` — how long a live socket's evidence of life
  stands, how rarely presence is written, and how many conversation streams
  one transition tells. See ``presence.py``: presence is a fact about the
  OTHER participant's connections, never about the reader's own socket.
- ``SUBJECT_TYPES`` — the OPEN subject-type registry (``subjects.py``), EMPTY
  out of the box. Each policy names the ``card_function`` that renders that
  subject. A marketplace declares ``listing`` here; a generic chat declares
  nothing and every thread is about nothing in particular.
- ``SEARCH_NAME_FIELDS`` / ``SEARCH_SUBJECT_SCAN`` — how ``GET
  /conversations?search=`` finds a thread by WHO it is with and WHAT it is
  about. The first names the user-model fields a display name is made of (the
  default is what ``stapel_core``'s user profile presenter renders); the
  second bounds how many subject cards one search may resolve.
- ``SYSTEM_LINE_LABELS`` — the words a system marker draws as on a
  conversation-list row (``last_message.body_preview``), and therefore also
  the words that find it. Empty by default: the marker is the host's to
  render, and an unlabelled one is drawn by nobody and found by nobody rather
  than printing ``video.call.ended:188`` at a reader.
- ``BLOCK_ENFORCEMENT`` / ``BLOCK_FUNCTION`` — whether a blocked party may
  OPEN a direct thread with the other party or send into one, and who is
  asked. See ``blocks.py``: a provider that is present and failing is a 503,
  never an admission — and a thread that already exists is still returned to
  both of them, because a block does not delete history.
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
    # OPEN subject-type registry (subjects.py). EMPTY builtins: a messaging
    # engine has no subject types of its own, and the one that would go here
    # (`listing`) belongs to whoever owns listings. Merge over builtins, None
    # removes. Each policy names a `card_function` — the batched comm Function
    # ({keys} -> {cards}) that turns a subject key into a renderable card.
    # A dict axis whose VALUES are policies, not a flag.
    "SUBJECT_TYPES": {},
    # Seconds to wait for a subject card before rendering the conversation
    # without one. A header is never worth blocking a thread on.
    "SUBJECT_CARD_TIMEOUT_S": 2.0,
    # How long a live socket's evidence of life stands without renewal (a
    # "tuning" int axis). Presence is the AND of a connection count and this
    # lease; the lease is what a worker killed mid-socket cannot leave behind.
    # Keep it comfortably ABOVE PRESENCE_WRITE_THROTTLE_S or a busy socket
    # would let its own lease lapse between throttled writes.
    "PRESENCE_TTL_S": 90,
    # Least seconds between two presence writes for one user. Every inbound
    # frame is evidence of life and a heartbeat is inbound every few seconds;
    # without this, presence would be a write-per-pong. 0 disables the
    # throttle (every touch writes).
    "PRESENCE_WRITE_THROTTLE_S": 30,
    # Most conversation streams one presence transition fans out to, newest
    # first. A user with ten thousand dormant threads must not turn one socket
    # close into ten thousand signals; a thread past the bound repaints from
    # the participant's presence on its next REST read. 0 turns the live
    # announcement off and leaves presence a REST-only fact.
    "PRESENCE_FANOUT_LIMIT": 200,
    # Whether presence is disclosed only to participants who have an ACCOUNT.
    # A storefront mints a guest the moment somebody taps "message the
    # seller": a stored user that passes IsAuthenticated while nobody has
    # registered. True (the default) answers such a session with the offline
    # default and sends it no flip — "last seen 38 minutes ago" is a fact
    # about the counterpart's day, and a tap is not the moment to hand it to
    # the open internet. The account on the other side of the thread keeps
    # both. False is the pre-0.8.0 answer, stated rather than inherited. On a
    # user model with no guests this changes nothing.
    "PRESENCE_REQUIRES_ACCOUNT": True,
    # The user-model field paths a counterpart's DISPLAY NAME is made of, for
    # `GET /conversations?search=`. The default is the one stapel-core's
    # UserProfilePresenter renders (`display_name` sources `username`) plus the
    # two name halves AbstractUser carries. A deployment that swapped the
    # presenter — or that keeps names in a profile table — names its own paths
    # here (`profile__display_name` traverses, as anywhere in the ORM), because
    # a search that matched a field nobody renders would find rows by text that
    # is nowhere on the screen. An empty list searches no name at all; a path
    # that does not resolve is a boot error (stapel_chat.E021) rather than a
    # 500 on the first search.
    "SEARCH_NAME_FIELDS": ["username", "first_name", "last_name"],
    # How many of the caller's most-recently-updated SUBJECT threads one search
    # may resolve cards for. The subject's title lives in whoever owns the
    # subject, not here, so matching it costs one batched card call per subject
    # type (SUBJECT_CARD_TIMEOUT_S each) over this many keys — never one call
    # per row, and never an unbounded key list handed to a catalogue. Threads
    # past the bound are still found by name and by their last line; only the
    # title match stops, and the truncation is logged rather than guessed at.
    # 0 turns title matching off entirely (a deployment whose provider is slow,
    # or that has no subjects, pays nothing).
    "SEARCH_SUBJECT_SCAN": 500,
    # The words a SYSTEM marker draws as on a conversation-list row — an OPEN
    # dict axis, marker -> short label, EMPTY out of the box. A system line's
    # body is machine vocabulary the host renders ("chat.support.resolved",
    # "video.call.ended:188"), and this module owns those words in no language,
    # so it ships none: an unlabelled marker gives `last_message.body_preview`
    # null (the row draws its own phrase off `kind`) and is matched by no
    # search, because a row must never be findable by text nobody can see. A
    # The markers this module itself posts are `chat.support.assigned`,
    # `chat.support.resolved`, `chat.support.reopened` and
    # `chat.participant.left:<user_id>` — a deployment that wants any of them
    # drawn names it here. A
    # deployment that DOES name a label gets both halves at once — the label is
    # what the row draws AND what the search matches, one rule
    # (services.drawn_last_line). A marker may carry an argument after a colon;
    # the label is looked up on the exact body, then on the part before the
    # colon, and is static text (the argument is never interpolated).
    "SYSTEM_LINE_LABELS": {},
    # Whether a block stops opening a NEW direct thread and sending into one.
    # It never stops `create_direct` from RETURNING a thread that already
    # exists: that is a read of history, and this fleet's blocks do not
    # delete history.
    #   "auto"     — enforce when a block provider is reachable; when there is
    #                none, this deployment has no blocks and W003 says so at
    #                every boot.
    #   "required" — a deployment that HAS blocks and refuses to run without
    #                them: an unreachable provider is E017 at check time and a
    #                503 at both doors, never an allowed message and never a
    #                thread opened anyway.
    #   "off"      — deliberately not enforced (W004). A choice on the record.
    "BLOCK_ENFORCEMENT": "auto",
    # The comm Function asked, BY NAME — never an import. stapel-profiles owns
    # blocks; `profiles.relationships` takes {"pairs": [[a, b], ...]} and
    # answers {"blocked": [[a, b], ...]}, blocked in either direction.
    "BLOCK_FUNCTION": "profiles.relationships",
    # Seconds to wait for the block answer. Timing out is an outage, and an
    # outage is a 503 — never a delivered message.
    "BLOCK_TIMEOUT_S": 2.0,
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
