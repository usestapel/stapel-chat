"""The attachment type registry, and the render metadata a UI needs up front.

**stapel-cdn owns this metadata; this module consumes it.** Since stapel-cdn
0.16.0 the whole snapshot — aspect, byte size, the 16px WebP preview, the video
poster, the voice waveform, and a *named reason* whenever one of them is
absent — arrives from one comm call. Nothing here re-derives any of it:
transcoding a preview or measuring a duration is the CDN's job, and a second
implementation would be a second answer to "how big is this picture".

One vocabulary, not two
-----------------------
The attachment ``type`` names are **the CDN's ``kind`` names**: ``image``,
``gif``, ``video``, ``audio``, ``file``, plus whatever a host registers in
both. They were briefly allowed to drift (chat said ``voice`` where the CDN
said ``audio``) and that is precisely the seam defect this fleet keeps paying
for — two names for one thing, agreeing by comment. They agree by construction
now, and a test asserts it.

**The registry is OPEN**, merge-over-builtins: builtins <- ``STAPEL_CHAT
["ATTACHMENT_TYPES"]`` <- :func:`register_attachment_type`, later wins, a value
of ``None`` REMOVES a type. The CDN's ``MEDIA_KINDS`` is the same shape, so a
host adds stickers to both with two dict literals and no fork.

The preview pair
----------------
``preview_b64`` is the bytes; ``preview_kind`` is what they depict — ``blur``
(a 16px LQIP), ``poster`` (a video frame), ``waveform`` (an amplitude strip) or
``null`` (a document: there is nothing to show but an icon). They are **two
fields on purpose**. ``preview_kind`` is known from the type alone, so a client
knows what shape of placeholder to reserve *before* the preview exists —
which is the entire point of not jumping the layout. Collapsing them into one
nullable field throws that away.

Degradation is data, never an exception
---------------------------------------
``meta_status`` (``ok`` / ``partial`` / ``missing``) and ``meta_reason`` travel
with every attachment, so a client can tell "still generating" from "this
deployment has no ffmpeg" and draw the right placeholder for each. In the same
spirit, a ref the CDN cannot resolve comes back in ``missing`` rather than
raising: a message with one dead attachment still renders the other nine.
``duration_ms`` is ``null`` when unmeasured and never ``0`` — a zero-length
voice message and an unmeasured one are different facts.

The bytes never travel through this module (models.py: no file storage). An
attachment is an opaque CDN ref plus the snapshot the CDN computed for it.
"""
from __future__ import annotations

import logging
import mimetypes
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: Every normalized attachment carries these keys, ``None`` where unknown, so
#: a client never has to test for their presence. Type-specific keys beyond
#: this set are declared by the registry entry's ``fields``.
BASE_FIELDS = (
    "key",
    "type",
    "mime",
    "bytes",
    "name",
    "ext",
    "width",
    "height",
    "aspect",
    "square",
    "animated",
    "duration_ms",
    "preview_b64",
    "preview_kind",
    "poster_url",
    "meta_status",
    "meta_reason",
    "variants",
)

#: Builtin attachment types — **the same names as stapel-cdn's builtin media
#: kinds** (``stapel_cdn.kinds.BUILTIN_MEDIA_KINDS``), because they name the
#: same thing and two names for one thing is how seams rot.
#:
#: ``fields`` is what a UI may *expect* populated for this type — a rendering
#: contract, not a validation rule: the CDN generates previews asynchronously,
#: so an image whose ``preview_b64`` is not ready yet must still be sendable
#: (it arrives with ``meta_status: "partial"`` and a named reason).
#: ``preview_kind`` is what this type's ``preview_b64`` will depict, known
#: from the type alone so a client can reserve the right placeholder before
#: any preview exists. ``media`` marks the types whose ref resolves to a CDN
#: asset worth describing.
BUILTIN_ATTACHMENT_TYPES: dict[str, Optional[dict]] = {
    "image": {
        "fields": (
            "mime", "bytes", "width", "height", "aspect", "square",
            "preview_b64", "variants",
        ),
        "preview_kind": "blur",
        "media": True,
    },
    # A GIF is an image on the wire and an animation on the screen; it gets
    # its own type so a client can offer a play affordance without sniffing
    # mime. `animated` is true for it in the CDN's registry too.
    "gif": {
        "fields": (
            "mime", "bytes", "width", "height", "aspect", "animated",
            "preview_b64", "variants",
        ),
        "preview_kind": "blur",
        "media": True,
    },
    "video": {
        "fields": (
            "mime", "bytes", "width", "height", "aspect", "duration_ms",
            "preview_b64", "poster_url", "variants",
        ),
        "preview_kind": "poster",
        "media": True,
    },
    # A voice message. Its `preview_b64` is a rendered waveform IMAGE — the
    # client paints one <img>, not a canvas loop over a float array. Named
    # `audio` and not `voice`: that is the CDN's kind for it, and the two
    # registries are one vocabulary.
    "audio": {
        "fields": ("mime", "bytes", "duration_ms", "preview_b64"),
        "preview_kind": "waveform",
        "media": True,
    },
    # A document. Extension and mime are the whole render: an icon and a name.
    # There is nothing to preview, so `preview_kind` is None.
    "file": {
        "fields": ("mime", "bytes", "name", "ext"),
        "preview_kind": None,
        "media": True,
    },
}

#: Runtime overrides, kept out of the settings layer so a test resets without
#: touching Django settings.
_runtime_types: dict[str, Optional[dict]] = {}


class UnknownAttachmentType(Exception):
    """An attachment declares a type no layer of the registry provides."""


class InvalidAttachment(Exception):
    """An attachment is malformed (no key, oversized preview, wrong shape)."""


class AttachmentNotOwned(Exception):
    """An attached private object does not belong to the sender.

    ``index`` is the attachment's position in the message as sent. A ref
    that does not exist is refused the same way, so the answer never tells
    a sender whether somebody else's object exists.
    """

    def __init__(self, index: int):
        self.index = index
        super().__init__(f"attachment {index} is not the sender's to share")


def register_attachment_type(name: str, spec: Optional[dict]) -> None:
    """Register/override an attachment type at runtime.

    ``spec=None`` removes a type a lower layer provided — the way a
    deployment that must not accept voice messages says so.
    """
    _runtime_types[name] = spec


def reset_attachment_types() -> None:
    """Tests only: drop runtime attachment-type overrides."""
    _runtime_types.clear()


def get_attachment_types() -> dict[str, dict]:
    """Effective registry: builtins <- settings <- runtime, ``None`` removes."""
    from .conf import chat_settings

    merged: dict[str, Optional[dict]] = dict(BUILTIN_ATTACHMENT_TYPES)
    for source in (chat_settings.ATTACHMENT_TYPES or {}, _runtime_types):
        for name, spec in source.items():
            merged[name] = spec
    return {name: spec for name, spec in merged.items() if spec is not None}


def attachment_type_names() -> tuple[str, ...]:
    """Sorted names of every live attachment type (the contract artifact's view)."""
    return tuple(sorted(get_attachment_types()))


# ── normalization ────────────────────────────────────────────────────────


def _guess_ext(name: str | None, mime: str | None) -> str | None:
    if name:
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        if ext:
            return ext
    if mime:
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return guessed.lstrip(".").lower()
    return None


def _check_data_uri(value, field: str, limit: int):
    """A base64 preview is untrusted bytes on their way to other people's
    screens. Bound the size and pin the shape; anything else is refused."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidAttachment(f"{field} must be a data: URI string")
    if not value.startswith("data:image/"):
        raise InvalidAttachment(f"{field} must be a data:image/... URI")
    if len(value.encode("utf-8")) > limit:
        raise InvalidAttachment(f"{field} exceeds {limit} bytes")
    return value


def normalize_attachment(raw) -> dict:
    """One inbound attachment -> the canonical dict every response carries.

    Accepts the pre-0.3 bare-string form (``"product/<hash>"``) and treats it
    as ``{"key": ..., "type": "file"}``: an old client keeps sending, it just
    does not get a typed render.
    """
    from .conf import chat_settings

    if isinstance(raw, str):
        raw = {"key": raw, "type": "file"}
    if not isinstance(raw, dict):
        raise InvalidAttachment("an attachment must be an object or a ref string")

    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        raise InvalidAttachment("an attachment needs a non-empty 'key'")

    kind = raw.get("type") or "file"
    if kind not in get_attachment_types():
        raise UnknownAttachmentType(kind)

    limit = int(chat_settings.MAX_PREVIEW_B64_BYTES)
    spec = get_attachment_types()[kind] or {}
    out = dict(raw)
    out["key"] = key.strip()
    out["type"] = kind
    out["preview_b64"] = _check_data_uri(raw.get("preview_b64"), "preview_b64", limit)
    for field in BASE_FIELDS:
        out.setdefault(field, None)
    # preview_kind follows from the TYPE, so it is known before any preview
    # exists — that is what lets a client reserve a waveform-shaped box for a
    # voice note whose waveform the CDN is still rendering. The client does not
    # get to assert it; the registry decides.
    out["preview_kind"] = spec.get("preview_kind")
    if out.get("ext") is None:
        out["ext"] = _guess_ext(out.get("name"), out.get("mime"))
    # Nothing has been asked of the CDN yet, so nothing is confirmed. A named
    # status from the start means no consumer ever meets an unexplained null.
    if out.get("meta_status") is None:
        out["meta_status"] = "missing"
        out["meta_reason"] = out.get("meta_reason") or "not_described"
    return out


# ── CDN enrichment (comm, never an import) ───────────────────────────────

#: Snapshot keys this module denormalizes onto an attachment. The CDN owns
#: every one of them. Anything else it returns is carried through untouched,
#: which is what lets a new CDN field reach a client with no release here.
_DESCRIBE_FIELDS = (
    "mime", "ext", "bytes", "width", "height", "aspect", "square", "animated",
    "duration_ms", "preview_b64", "preview_kind", "poster_url",
    "meta_status", "meta_reason", "variants",
)

#: Keys whose ``None`` from the CDN is a FACT, not an absence, and must
#: overwrite whatever the client claimed.
#:
#: ``duration_ms`` is the one that matters: the CDN returns ``null`` for
#: unmeasured and never ``0``, because a zero-length voice message and an
#: unmeasured one are different things a UI draws differently. If a sender's
#: optimistic guess survived a CDN ``null``, that distinction would be lost at
#: exactly the moment the authority said it did not know.
_AUTHORITATIVE_NULLS = ("duration_ms", "preview_b64", "preview_kind", "poster_url")

#: ``cdn.describe_many`` refuses more than this per call, because each snapshot
#: may inline a preview and so the batch size IS the response size. Mirrored
#: rather than imported — cross-module is comm by string name.
DESCRIBE_BATCH_LIMIT = 50


def _apply_snapshot(attachment: dict, snapshot: dict) -> dict:
    """Merge one CDN snapshot over one normalized attachment."""
    if not isinstance(snapshot, dict):
        return attachment
    out = dict(attachment)
    for field, value in snapshot.items():
        if value is None and field not in _AUTHORITATIVE_NULLS:
            continue
        if field in _DESCRIBE_FIELDS or field not in BASE_FIELDS:
            out[field] = value
    if out.get("ext") is None:
        out["ext"] = _guess_ext(out.get("name"), out.get("mime"))
    return out


def _unresolved(attachment: dict) -> dict:
    """Mark an attachment the CDN could not resolve — as data, never as an
    exception. A message with one dead attachment still renders the rest."""
    out = dict(attachment)
    out["meta_status"] = "missing"
    out["meta_reason"] = "unknown_ref"
    return out


def describe_attachments(attachments: list[dict]) -> list[dict]:
    """Merge the CDN's render snapshots over a whole list, in ONE comm call.

    ``cdn.describe_many`` resolves a page of refs with one query per model, so
    a message's attachments cost one round trip rather than N — the per-ref
    call was the only thing left making it N.

    Best-effort by design. An unreachable CDN, an unwired comm transport, a
    provider that is simply not installed: all leave the client-supplied
    fields in place and log. A chat message must not fail to send because a
    metadata provider blinked; the worst case is a bubble that renders from
    the sender's own numbers. A ref the CDN *can* answer about but does not
    know is a different case, and it comes back as ``meta_status: "missing"``
    on that one attachment.
    """
    describable = [
        a for a in attachments
        if (get_attachment_types().get(a.get("type")) or {}).get("media")
    ]
    if not describable:
        return attachments

    refs = list(dict.fromkeys(a["key"] for a in describable))
    snapshots, missing, _unanswered = _fetch_snapshots(refs)

    out = []
    for attachment in attachments:
        key = attachment.get("key")
        if key in snapshots:
            out.append(_apply_snapshot(attachment, snapshots[key]))
        elif key in missing:
            out.append(_unresolved(attachment))
        else:
            out.append(attachment)
    return out


def describe_attachment(attachment: dict) -> dict:
    """:func:`describe_attachments` for a single attachment."""
    return describe_attachments([attachment])[0]


def prepare_attachments(raws, *, sender=None, conversation=None) -> list[dict]:
    """Normalize, then enrich, a whole inbound attachment list.

    What comes back is the STORED form: with CDN metadata on, every media
    attachment is reduced to :func:`reference_form` — the ref and the facts
    that do not expire. The URLs in the snapshot are not kept (see
    :func:`resolve_messages`).

    With a ``sender``, every attached private object must be the sender's
    (:func:`verify_ownership`) before anything is described or stored.
    """
    from .conf import chat_settings

    items = list(raws or [])
    if len(items) > int(chat_settings.MAX_ATTACHMENTS):
        raise InvalidAttachment(
            f"at most {chat_settings.MAX_ATTACHMENTS} attachments per message"
        )
    normalized = [normalize_attachment(item) for item in items]
    if not _cdn_metadata():
        return normalized
    if sender is not None:
        verify_ownership(normalized, sender=sender, conversation=conversation)
    return [reference_form(a) for a in describe_attachments(normalized)]


# ── who may attach what ──────────────────────────────────────────────────
#
# Every read resolves an attachment's URLs for whoever may read the message,
# so attaching a ref is sharing its bytes with the thread. A private object
# (a voice note, a document) is the sender's to share only if the sender
# uploaded it. stapel-cdn answers that with ``cdn.ownership`` (0.31.0+); a
# CDN without it is a CDN this module cannot ask, and the send then proceeds
# as before and is counted as unverified rather than refused, so a deployment
# on an older CDN keeps working (``checks`` W006 says the check is inactive).

#: Visibilities the sender must own. ``public`` objects (images, video) are
#: served on the public media route to anyone holding the ref, so attaching
#: one shares nothing that was not already shared.
OWNED_VISIBILITIES = frozenset({"private", "protected"})

_UNVERIFIED = {"total": 0}


def ownership_unverified_total() -> int:
    """Sends whose attachments could not be checked, since process start.
    Every increment also logs a WARNING naming the reason."""
    return _UNVERIFIED["total"]


def _count_unverified(reason: str, refs) -> None:
    _UNVERIFIED["total"] += 1
    logger.warning(
        "chat: attachment ownership NOT verified (%s) for %d ref(s); the send "
        "proceeds. stapel-cdn >= 0.31.0 provides cdn.ownership.",
        reason, len(refs),
    )


def _shared_in_thread(conversation, refs: set[str]) -> set[str]:
    """The refs already on a live message of ``conversation``.

    Every participant can already resolve those, so attaching one again
    (quoting a document, an operator forwarding a customer's file within the
    support thread) discloses nothing new. Only refs that failed the
    ownership check reach this, so the scan runs on the refusal path only.
    """
    if conversation is None or not refs:
        return set()
    from .models import Message

    found: set[str] = set()
    rows = (
        Message.objects.filter(conversation_id=conversation.pk, deleted_at__isnull=True)
        .exclude(attachments=[])
        .values_list("attachments", flat=True)
    )
    for attachments in rows.iterator(chunk_size=500):
        for item in attachments or []:
            key = item.get("key") if isinstance(item, dict) else item
            if key in refs:
                found.add(key)
        if found == refs:
            break
    return found


def verify_ownership(attachments: list[dict], *, sender, conversation=None) -> None:
    """Refuse a send that shares a private object the sender does not own.

    One ``cdn.ownership`` call for every media ref of the message. Raises
    :class:`AttachmentNotOwned` naming the first offending index when a
    private or protected ref is neither the sender's nor already shared in
    this conversation, or does not exist at all. Public refs pass. A CDN
    that does not answer (no such Function before stapel-cdn 0.31.0, an
    outage, a malformed answer) leaves the send as it was and is counted
    (:func:`ownership_unverified_total`).
    """
    indexed = [
        (i, a["key"]) for i, a in enumerate(attachments)
        if _is_media(a) and isinstance(a.get("key"), str)
    ]
    if not indexed or sender is None or getattr(sender, "pk", None) is None:
        return
    refs = list(dict.fromkeys(ref for _, ref in indexed))
    try:
        from stapel_core.comm import call

        answer = call("cdn.ownership", {"refs": refs, "owner_id": str(sender.pk)}) or {}
        items = {
            item.get("ref"): item
            for item in (answer.get("items") or [])
            if isinstance(item, dict)
        }
    except Exception as exc:  # noqa: BLE001 - absent or failing provider
        _count_unverified(type(exc).__name__, refs)
        return

    refused: set[str] = set()
    unanswered: list[str] = []
    for ref in refs:
        item = items.get(ref)
        if item is None or "owned" not in item:
            unanswered.append(ref)
        elif not item.get("exists"):
            refused.add(ref)
        elif item.get("visibility") in OWNED_VISIBILITIES and item.get("owned") is not True:
            refused.add(ref)
    if unanswered:
        _count_unverified("incomplete answer", unanswered)
    if refused:
        refused -= _shared_in_thread(conversation, refused)
    for index, ref in indexed:
        if ref in refused:
            raise AttachmentNotOwned(index)


# ── references at rest, URLs at read ─────────────────────────────────────
#
# A CDN URL is not a fact about an attachment; it is a short-lived grant. A
# private object (a voice note, a document) is served by a signed link that
# expires after the CDN's SIGNED_MEDIA_TTL_SECONDS, so a URL written into a
# message row is dead minutes after the send. The row therefore keeps the ref
# and the non-expiring metadata, and every read resolves URLs afresh, after
# the reader's access to the message has been decided by the caller. The CDN
# signs for whoever asks it with a ref: which reader may see an attachment is
# this module's decision, and every caller of resolve_messages makes it first.

#: Fields that hold a URL, or a structure of them, on a media attachment.
#: ``variants`` entries carry one ``url`` each; ``href`` is this module's own
#: stable link, derived per read and never stored.
URL_FIELDS = frozenset({"url", "poster_url", "clean_url", "variants", "href"})

#: ``meta_reason`` of a media attachment whose ref the CDN did not answer
#: about on this read (unreachable, timed out, not installed). The stored
#: metadata still renders; only the URLs are absent.
REASON_CDN_UNAVAILABLE = "cdn_unavailable"

_CACHE_PREFIX = "stapel_chat:attachment-snapshot:"


def _cdn_metadata() -> bool:
    from .conf import chat_settings

    return str(chat_settings.ATTACHMENT_METADATA) == "cdn"


def _is_media(attachment: dict) -> bool:
    return bool(
        (get_attachment_types().get(attachment.get("type")) or {}).get("media")
    )


def _is_url_field(name: str) -> bool:
    return name in URL_FIELDS or name.endswith("_url")


def reference_form(attachment) -> dict:
    """The stored shape of one attachment: everything but its URLs.

    A media attachment loses every URL-bearing field; a host-registered
    non-media type (``media: False``) is not a CDN object and is kept as it
    came. The pre-0.3 bare-string form is read as a ``file`` ref.
    """
    if isinstance(attachment, str):
        attachment = {"key": attachment, "type": "file"}
    if not isinstance(attachment, dict):
        return {}
    if not _is_media(attachment):
        return dict(attachment)
    return {k: v for k, v in attachment.items() if not _is_url_field(k)}


def _cache_seconds() -> int:
    from .conf import chat_settings

    try:
        return max(0, int(chat_settings.ATTACHMENT_URL_CACHE_S or 0))
    except (TypeError, ValueError):
        return 0


def _fetch_snapshots(refs: list[str]) -> tuple[dict, set, set]:
    """``(snapshots, missing, unanswered)`` for ``refs``.

    Cached snapshots are served first; the rest go to ``cdn.describe_many`` in
    pages of :data:`DESCRIBE_BATCH_LIMIT`. A page whose call fails leaves its
    refs in ``unanswered`` and does not stop the other pages.
    """
    from django.core.cache import cache

    snapshots: dict[str, dict] = {}
    missing: set[str] = set()
    unanswered: set[str] = set()
    ttl = _cache_seconds()
    pending = list(refs)
    if ttl and pending:
        try:
            hits = cache.get_many([_CACHE_PREFIX + r for r in pending])
        except Exception:
            hits = {}
        for ref in pending:
            snapshot = hits.get(_CACHE_PREFIX + ref)
            if isinstance(snapshot, dict):
                snapshots[ref] = snapshot
        pending = [r for r in pending if r not in snapshots]
    if not pending:
        return snapshots, missing, unanswered

    try:
        from stapel_core.comm import call
    except Exception:  # pragma: no cover - stapel-core is a hard dependency
        return snapshots, missing, set(pending)

    fresh: dict[str, dict] = {}
    for start in range(0, len(pending), DESCRIBE_BATCH_LIMIT):
        page = pending[start:start + DESCRIBE_BATCH_LIMIT]
        try:
            answer = call("cdn.describe_many", {"refs": page}) or {}
        except Exception:
            logger.info(
                "chat: cdn.describe_many unavailable for %d ref(s); "
                "attachments render without URLs", len(page), exc_info=True,
            )
            unanswered.update(page)
            continue
        items = answer.get("items") or {}
        page_missing = set(answer.get("missing") or [])
        for ref in page:
            if isinstance(items.get(ref), dict):
                fresh[ref] = items[ref]
            elif ref in page_missing:
                missing.add(ref)
            else:
                unanswered.add(ref)
    snapshots.update(fresh)
    if ttl and fresh:
        try:
            cache.set_many({_CACHE_PREFIX + r: s for r, s in fresh.items()}, ttl)
        except Exception:
            logger.debug("chat: attachment snapshot cache unavailable", exc_info=True)
    return snapshots, missing, unanswered


def _unavailable(attachment: dict) -> dict:
    out = dict(attachment)
    out["meta_status"] = "partial" if out.get("meta_status") == "ok" else (
        out.get("meta_status") or "missing"
    )
    out["meta_reason"] = REASON_CDN_UNAVAILABLE
    return out


def _with_url_defaults(attachment: dict) -> dict:
    out = dict(attachment)
    out.setdefault("poster_url", None)
    out.setdefault("variants", [])
    return out


def attachment_href(conversation_id, message_id, index: int):
    """This module's stable link to one attachment, or ``None`` unmounted.

    It does not expire: each request checks the caller's access to the
    message and redirects to a URL resolved at that moment
    (:class:`~stapel_chat.views.MessageAttachmentView`).
    """
    from django.urls import NoReverseMatch, reverse

    try:
        return reverse(
            "chat-message-attachment",
            args=[str(conversation_id), str(message_id), int(index)],
        )
    except NoReverseMatch:
        return None


def stored_attachments(message) -> list[dict]:
    """A message's attachments in reference form, each with its ``href``.

    For readers that keep what they are given past a signed link's lifetime
    (the ``chat.message`` Action on the bus). No CDN call is made.
    """
    if getattr(message, "deleted_at", None) is not None:
        return []
    out = []
    for index, raw in enumerate(message.attachments or []):
        item = reference_form(raw)
        if not item:
            continue
        if _is_media(item) and _cdn_metadata():
            item["href"] = attachment_href(message.conversation_id, message.pk, index)
        out.append(item)
    return out


def resolve_messages(messages) -> dict[str, list[dict]]:
    """Fresh attachments for a page of messages: ``{str(message.pk): [...]}``.

    The caller has already decided that the reader may see every message
    passed in; this function grants URLs to exactly those. Every media ref on
    the page is resolved in one batched describe (paged at the CDN's ceiling,
    so a page costs ``ceil(refs / 50)`` calls, never one per attachment) and
    merged over the stored reference form, so a row written before URLs
    stopped being stored — one that still carries a snapshot's URLs — is read
    exactly like a new one: its stored URLs are dropped, not served.

    A ref the CDN reports missing is ``meta_status: "missing"`` /
    ``unknown_ref``; one it did not answer about is
    ``meta_reason: "cdn_unavailable"`` with no URLs. Neither fails the read.
    A tombstone has no attachments.
    """
    messages = list(messages)
    stored: dict[str, list[dict]] = {}
    for message in messages:
        if getattr(message, "deleted_at", None) is not None:
            stored[str(message.pk)] = []
            continue
        items = []
        for index, raw in enumerate(message.attachments or []):
            if not isinstance(raw, (dict, str)):
                continue
            item = normalize_stored(raw)
            if _is_media(item) and _cdn_metadata():
                item = reference_form(item)
                item["href"] = attachment_href(
                    message.conversation_id, message.pk, index
                )
            items.append(item)
        stored[str(message.pk)] = items

    if not _cdn_metadata():
        return stored

    refs = list(dict.fromkeys(
        a["key"] for items in stored.values() for a in items
        if _is_media(a) and a.get("key")
    ))
    if not refs:
        return stored
    snapshots, missing, unanswered = _fetch_snapshots(refs)

    resolved: dict[str, list[dict]] = {}
    for message_id, items in stored.items():
        out = []
        for item in items:
            key = item.get("key")
            if not _is_media(item):
                out.append(item)
            elif key in snapshots:
                out.append(_with_url_defaults(_apply_snapshot(item, snapshots[key])))
            elif key in missing:
                out.append(_with_url_defaults(_unresolved(item)))
            else:
                out.append(_with_url_defaults(_unavailable(item)))
        resolved[message_id] = out
    return resolved


def resolve_message(message) -> list[dict]:
    """:func:`resolve_messages` for one message."""
    return resolve_messages([message])[str(message.pk)]


def normalize_stored(raw) -> dict:
    """A stored attachment as a dict, tolerating the pre-0.3 bare string."""
    if isinstance(raw, str):
        return {"key": raw, "type": "file"}
    return dict(raw)


__all__ = [
    "BASE_FIELDS",
    "BUILTIN_ATTACHMENT_TYPES",
    "InvalidAttachment",
    "UnknownAttachmentType",
    "DESCRIBE_BATCH_LIMIT",
    "REASON_CDN_UNAVAILABLE",
    "URL_FIELDS",
    "attachment_href",
    "attachment_type_names",
    "describe_attachment",
    "describe_attachments",
    "get_attachment_types",
    "normalize_attachment",
    "normalize_stored",
    "prepare_attachments",
    "reference_form",
    "register_attachment_type",
    "reset_attachment_types",
    "resolve_message",
    "resolve_messages",
    "stored_attachments",
]
