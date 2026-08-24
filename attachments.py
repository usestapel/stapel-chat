"""The attachment type registry, and the render metadata a UI needs up front.

Two things live here, and they are the same thing seen from two sides.

**The registry** (``ATTACHMENT_TYPES``) is a merge-over-builtins registry, the
same idiom as every other Stapel registry: builtins <- ``STAPEL_CHAT
["ATTACHMENT_TYPES"]`` <- :func:`register_attachment_type`, later wins, a
value of ``None`` REMOVES a type. It is deliberately **not** a closed enum,
because the next attachment kind is already named — stickers — and a closed
enum would make that a contract change instead of a settings line.

**The metadata** is what an attachment must carry so a chat bubble renders
*on first paint*: no second round trip, and no layout jump while the real
asset loads. That is one requirement, not a nicety — a message list that
reflows after every image is the visible symptom of an attachment contract
that shipped only a storage key.

    image / gif   aspect + bytes + a ~16px base64 micro-thumbnail
    video         aspect + bytes + duration + a poster (same micro-thumb slot)
    voice         duration + a base64 waveform image
    file          mime + extension (+ the original name)

The **bytes never travel through this module** (models.py: no file storage).
An attachment is an opaque CDN ref plus the render snapshot that the CDN
computed for it. This module *asks* for that snapshot by comm — ``call
("cdn.describe", {"ref": ...})`` — and never re-derives it: transcoding a
16px webp, measuring an aspect or drawing a waveform is stapel-cdn's job and
duplicating it here would be a second answer to "how big is this picture".

The CDN's answer is authoritative and is merged **over** whatever the client
sent. A client may pre-fill the same fields (it just uploaded the file and
knows them), which is what keeps a send working when the metadata provider is
unreachable: the send is not failed over a courtesy field.
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
    "duration_ms",
    "preview_b64",
    "waveform_b64",
    "variants",
)

#: Builtin attachment types. ``fields`` is what a UI may *expect* to be
#: populated for this type — it is a rendering contract, not a validation
#: rule: the CDN produces a micro-thumbnail asynchronously, so an image whose
#: ``preview_b64`` has not been generated yet must still be sendable.
#: ``media`` marks the types whose ref resolves to a CDN asset worth
#: describing.
BUILTIN_ATTACHMENT_TYPES: dict[str, Optional[dict]] = {
    "image": {
        "fields": ("mime", "bytes", "width", "height", "aspect", "preview_b64", "variants"),
        "media": True,
    },
    # A GIF is an image on the wire and an animation on the screen; it gets
    # its own type so a client can decide to autoplay without sniffing mime.
    "gif": {
        "fields": ("mime", "bytes", "width", "height", "aspect", "preview_b64", "variants"),
        "media": True,
    },
    "video": {
        "fields": (
            "mime", "bytes", "width", "height", "aspect", "duration_ms",
            "preview_b64", "variants",
        ),
        "media": True,
    },
    # A voice message. `waveform_b64` is the preview image drawn by the CDN,
    # not a float array — the client paints one <img>, not a canvas loop.
    "voice": {
        "fields": ("mime", "bytes", "duration_ms", "waveform_b64"),
        "media": True,
    },
    # A document. Extension and mime are the whole render: an icon and a name.
    "file": {
        "fields": ("mime", "bytes", "name", "ext"),
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
    out = dict(raw)
    out["key"] = key.strip()
    out["type"] = kind
    out["preview_b64"] = _check_data_uri(raw.get("preview_b64"), "preview_b64", limit)
    out["waveform_b64"] = _check_data_uri(raw.get("waveform_b64"), "waveform_b64", limit)
    for field in BASE_FIELDS:
        out.setdefault(field, None)
    if out.get("ext") is None:
        out["ext"] = _guess_ext(out.get("name"), out.get("mime"))
    return out


# ── CDN enrichment (comm, never an import) ───────────────────────────────

#: Keys of ``cdn.describe``'s render snapshot this module denormalizes. The
#: CDN owns every one of them; anything it returns that is not listed is
#: carried through untouched so a new CDN field reaches a client without a
#: release here.
_DESCRIBE_FIELDS = (
    "mime", "bytes", "width", "height", "aspect", "duration_ms",
    "preview_b64", "waveform_b64", "variants",
)


def describe_attachment(attachment: dict) -> dict:
    """Merge ``cdn.describe``'s snapshot over one normalized attachment.

    Best-effort by design. An unknown ref, an unreachable CDN, a comm
    transport that is not wired — all leave the client-supplied fields in
    place and log. A chat message must not fail to send because a metadata
    provider blinked; the worst case is a bubble that renders from the
    sender's own numbers.
    """
    spec = get_attachment_types().get(attachment.get("type")) or {}
    if not spec.get("media"):
        return attachment
    try:
        from stapel_core.comm import call

        snapshot = call("cdn.describe", {"ref": attachment["key"]}) or {}
    except Exception:
        logger.info(
            "chat: cdn.describe unavailable for %r; keeping client metadata",
            attachment.get("key"),
            exc_info=True,
        )
        return attachment
    if not isinstance(snapshot, dict):
        return attachment
    out = dict(attachment)
    for field, value in snapshot.items():
        if value is None:
            continue
        if field in _DESCRIBE_FIELDS or field not in BASE_FIELDS:
            out[field] = value
    if out.get("ext") is None:
        out["ext"] = _guess_ext(out.get("name"), out.get("mime"))
    return out


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
    return [describe_attachment(item) for item in normalized]


__all__ = [
    "BASE_FIELDS",
    "BUILTIN_ATTACHMENT_TYPES",
    "InvalidAttachment",
    "UnknownAttachmentType",
    "attachment_type_names",
    "describe_attachment",
    "get_attachment_types",
    "normalize_attachment",
    "prepare_attachments",
    "register_attachment_type",
    "reset_attachment_types",
]
