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

    snapshots: dict[str, dict] = {}
    missing: set[str] = set()
    refs = list(dict.fromkeys(a["key"] for a in describable))
    try:
        from stapel_core.comm import call

        for start in range(0, len(refs), DESCRIBE_BATCH_LIMIT):
            page = refs[start:start + DESCRIBE_BATCH_LIMIT]
            answer = call("cdn.describe_many", {"refs": page}) or {}
            snapshots.update(answer.get("items") or {})
            missing.update(answer.get("missing") or [])
    except Exception:
        logger.info(
            "chat: cdn.describe_many unavailable for %d ref(s); "
            "keeping client metadata", len(refs), exc_info=True,
        )
        return attachments

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


def prepare_attachments(raws) -> list[dict]:
    """Normalize, then enrich, a whole inbound attachment list."""
    from .conf import chat_settings

    items = list(raws or [])
    if len(items) > int(chat_settings.MAX_ATTACHMENTS):
        raise InvalidAttachment(
            f"at most {chat_settings.MAX_ATTACHMENTS} attachments per message"
        )
    normalized = [normalize_attachment(item) for item in items]
    if str(chat_settings.ATTACHMENT_METADATA) != "cdn":
        return normalized
    return describe_attachments(normalized)


__all__ = [
    "BASE_FIELDS",
    "BUILTIN_ATTACHMENT_TYPES",
    "InvalidAttachment",
    "UnknownAttachmentType",
    "DESCRIBE_BATCH_LIMIT",
    "attachment_type_names",
    "describe_attachment",
    "describe_attachments",
    "get_attachment_types",
    "normalize_attachment",
    "prepare_attachments",
    "register_attachment_type",
    "reset_attachment_types",
]
