"""Dataclass DTOs — the API models of stapel-chat (never ORM instances)."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ParticipantResponse:
    """A conversation participant.

    Attributes:
        user_id: The participant's user id.
        role: ``member`` or ``operator``.
        last_read_seq: seq of the newest message this participant has read.
        last_delivered_seq: seq of the newest message their client
            acknowledged receiving. Delivered is a weaker fact than read; a
            UI that draws one tick and two ticks needs both.
    """

    user_id: str
    role: str
    last_read_seq: int
    last_delivered_seq: int = 0


@dataclass
class AttachmentResponse:
    """One attachment, carrying everything a bubble needs to paint on first
    frame — no second round trip, and no reflow when the asset lands.

    Every field is present on every attachment, ``null`` where it does not
    apply or the CDN has not produced it yet; which of them a given ``type``
    is expected to populate is declared by the attachment-type registry
    (:mod:`stapel_chat.attachments`), which is open — ``sticker`` is a
    settings line, not a release.

    Attributes:
        key: Opaque CDN ref (``<type>/<hash>``). The only field this module
            stores by itself; the bytes are never here.
        type: Registry type — ``image`` / ``gif`` / ``video`` / ``voice`` /
            ``file`` out of the box, plus whatever the host registered.
        mime: Content type.
        bytes: Byte size of the original.
        name: Original filename (documents).
        ext: Lowercase extension without the dot (documents).
        width: Pixel width (image/gif/video).
        height: Pixel height (image/gif/video).
        aspect: ``width / height`` — the number that reserves the box before
            the image arrives, which is what stops the list jumping.
        duration_ms: Playback length (voice/video).
        preview_b64: ~16px webp micro-thumbnail as a ``data:`` URI — the
            blur-up placeholder, and the poster frame for a video.
        waveform_b64: Waveform image as a ``data:`` URI (voice).
        variants: Per-tier CDN geometry ``[{tier, branch, url, width,
            height}]`` for a responsive ``srcset``.
    """

    key: str
    type: str
    mime: Optional[str] = None
    bytes: Optional[int] = None
    name: Optional[str] = None
    ext: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    aspect: Optional[float] = None
    duration_ms: Optional[int] = None
    preview_b64: Optional[str] = None
    waveform_b64: Optional[str] = None
    variants: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class MessageResponse:
    """A single message — or its tombstone.

    A deleted message is still returned: same ``id``, same ``seq``, empty
    ``body``, empty ``attachments``, ``deleted=true``. That is deliberate, and
    it is what a client cache needs — an id that stops arriving is an id
    nobody can purge.

    Attributes:
        id: Message id (UUID).
        conversation_id: Owning conversation id.
        sender_id: Author's user id (null for system messages).
        seq: Position in the thread. Immutable — sort by this.
        rev_seq: Position in the conversation's revision journal; changes on
            every edit/delete. The realtime resume cursor, never a sort key.
        kind: ``text`` or ``system``.
        body: Message text (empty when only attachments are present, and
            always empty on a tombstone).
        reply_to: Quoted message id, if any.
        attachments: Render descriptors (see :class:`AttachmentResponse`).
        client_msg_id: The sender's own idempotency key, echoed back so an
            optimistic bubble can be reconciled with the real row.
        edited: Whether the body was changed after posting.
        edited_at: When it was changed (null if never).
        deleted: Whether this is a tombstone.
        deleted_at: When it was deleted (null if live).
        created_at: Creation time (tz-aware ISO 8601).
    """

    id: str
    conversation_id: str
    seq: int
    rev_seq: int
    kind: str
    body: str
    created_at: datetime
    sender_id: Optional[str] = None
    reply_to: Optional[str] = None
    attachments: List[AttachmentResponse] = field(default_factory=list)
    client_msg_id: Optional[str] = None
    edited: bool = False
    edited_at: Optional[datetime] = None
    deleted: bool = False
    deleted_at: Optional[datetime] = None


@dataclass
class ConversationResponse:
    """A conversation (thread).

    Attributes:
        id: Conversation id (UUID).
        kind: ``direct`` / ``group`` / ``support``.
        scope_key: Opaque host scope (workspace/org/tenant).
        support_status: ``open`` / ``pending`` / ``resolved`` (empty for
            direct/group).
        assigned_operator_id: Assigned support operator, if any.
        last_seq: High-water mark of message seq in this conversation.
        unread_count: Messages newer than the requesting user's read marker,
            authored by others.
        stream_key: The realtime stream this conversation lives on
            (``chat:conv:<id>``). Present on every conversation, always — a
            client is never left to guess where the live path is, and a UI
            that ignores this field is visibly ignoring something rather than
            silently defaulting to a timer.
        socket_path: Where to open it (``ws/chat/<id>``), relative to the
            deployment's WebSocket prefix.
        participants: The conversation's participants.
        created_at: Creation time.
        updated_at: Last-activity time.
    """

    id: str
    kind: str
    scope_key: str
    support_status: str
    last_seq: int
    unread_count: int
    created_at: datetime
    updated_at: datetime
    stream_key: str = ""
    socket_path: str = ""
    assigned_operator_id: Optional[str] = None
    participants: List[ParticipantResponse] = field(default_factory=list)


# ── Request DTOs ────────────────────────────────────────────────────────


@dataclass
class CreateConversationRequest:
    """Create a conversation.

    Attributes:
        kind: ``direct`` / ``group`` / ``support``. For ``direct`` supply
            exactly one other participant; ``support`` opens a thread for the
            requesting user (no other participants).
        participant_ids: Other participants to add (the requesting user is
            always a participant).
        scope_key: Ignored — the scope is resolved server-side from the
            SCOPE_PROVIDER seam; present for symmetry only.
    """

    kind: str = "direct"
    participant_ids: List[str] = field(default_factory=list)
    scope_key: str = ""


@dataclass
class SendMessageRequest:
    """Post a message.

    There is deliberately **no draft**. A compose box is client state; the
    server has one verb, and it is "send". A server-side draft concept would
    put a save round trip between the Enter key and the message, which is
    precisely the thing that must not exist.

    Attributes:
        body: Message text (required unless attachments are supplied).
        attachments: Render descriptors, or bare CDN ref strings for the
            pre-0.3 form.
        reply_to: Message id being replied to (must be in this conversation).
        client_msg_id: Sender-generated idempotency key. Retrying the same
            send with the same id returns the first message instead of
            posting a second one.
    """

    body: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    reply_to: Optional[str] = None
    client_msg_id: str = ""


@dataclass
class EditMessageRequest:
    """Replace a message's body. Author only.

    Attributes:
        body: The new text. An edit cannot empty a message — deleting is the
            verb for that, and it leaves a tombstone.
    """

    body: str


@dataclass
class MarkReadRequest:
    """Advance the requesting user's read/delivery markers.

    Attributes:
        upto_seq: seq of the newest message now considered read.
        delivered_upto_seq: seq of the newest message the client holds. Omit
            (or 0) to leave the delivery marker alone. Both markers only ever
            move forward.
    """

    upto_seq: int
    delivered_upto_seq: int = 0


@dataclass
class ActivityRequest:
    """Announce what the caller is doing right now.

    Attributes:
        state: A name from the open activity registry — ``typing``,
            ``recording_audio``, ``sending_video``, ``uploading_file``,
            ``idle``, plus whatever the host registered.
    """

    state: str
