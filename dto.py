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
        online: Whether THIS participant is connected right now — a
            server-side fact derived from their own sockets
            (:mod:`stapel_chat.presence`), never from the reader's. A client
            that renders "online" from its own socket state is stating that
            its own network is up and labelling it with somebody else's name;
            that is the bug this field exists to delete.
        last_seen_at: When they were last connected or active. What a header
            renders when ``online`` is false ("last seen 5 minutes ago").
            ``null`` means this deployment has never seen them connect —
            distinct from "seen long ago", so a UI can say nothing rather
            than invent a date.
        online_until: When this ``online`` stops being believable — the lease
            deadline (:mod:`stapel_chat.presence`). **A reader must stop
            treating ``online`` as true at this instant**, exactly as the
            server does. It is here because a lease running out is a SILENT
            transition: a flip is announced from a disconnect, and a socket
            that dies without one (a killed tab, a lost worker) produces no
            event at all — the server heals on its own clock, and a client
            that cannot see the deadline never learns. ``null`` when this
            deployment has never seen them connect.
    """

    user_id: str
    role: str
    last_read_seq: int
    last_delivered_seq: int = 0
    online: bool = False
    last_seen_at: Optional[datetime] = None
    online_until: Optional[datetime] = None


@dataclass
class AttachmentResponse:
    """One attachment, carrying everything a bubble needs to paint on first
    frame — no second round trip, and no reflow when the asset lands.

    Every field is present on every attachment, ``null`` where it does not
    apply or the CDN has not produced it yet; which of them a given ``type``
    is expected to populate is declared by the attachment-type registry
    (:mod:`stapel_chat.attachments`), which is open — a sticker is a settings
    line, not a release.

    The names are **stapel-cdn's** (`cdn.describe`), because they name the
    same things and two vocabularies for one thing is how a seam rots.

    Attributes:
        key: Opaque CDN ref (``<type>/<hash>``). The only field this module
            stores by itself; the bytes are never here.
        type: Registry type, and the CDN's media ``kind``: ``image`` / ``gif``
            / ``video`` / ``audio`` / ``file`` out of the box, plus whatever
            the host registered in both.
        mime: Content type.
        bytes: Byte size of the original.
        name: Original filename (documents).
        ext: Lowercase extension (documents).
        width: Pixel width (image/gif/video).
        height: Pixel height (image/gif/video).
        aspect: ``width / height`` — the number that reserves the box before
            the image arrives, which is what stops the list jumping.
        square: Whether the asset is square within a pixel.
        animated: Whether it moves, so a UI offers a play affordance rather
            than treating it as a still.
        duration_ms: Playback length (audio/video). ``null`` means
            **unmeasured**, never zero — a zero-length voice message and an
            unmeasured one are different facts.
        preview_b64: The inline placeholder as a ``data:image/webp;base64,…``
            URI, bounded by the CDN's byte budget.
        preview_kind: What ``preview_b64`` depicts — ``blur`` (a 16px LQIP),
            ``poster`` (a video frame), ``waveform`` (a voice amplitude
            strip), or ``null`` (a document: nothing but an icon). It is a
            **separate field on purpose**: it follows from ``type``, so it is
            known before any preview exists and a client can reserve the
            right-shaped placeholder immediately.
        poster_url: A video's full poster image, present only once the poster
            actually exists — never derived from the hash.
        meta_status: ``ok`` / ``partial`` / ``missing``.
        meta_reason: Why, whenever the status is not ``ok``
            (``ffmpeg_missing``, ``not_generated``, ``preview_over_budget``,
            ``unknown_ref``, …). A degraded attachment stays renderable: this
            is what lets a client tell "still generating" from "this
            deployment has no ffmpeg" and draw the right placeholder.
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
    square: Optional[bool] = None
    animated: Optional[bool] = None
    duration_ms: Optional[int] = None
    preview_b64: Optional[str] = None
    preview_kind: Optional[str] = None
    poster_url: Optional[str] = None
    meta_status: str = "missing"
    meta_reason: Optional[str] = None
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
class SubjectResponse:
    """What a conversation is about, and the card somebody else rendered.

    The ``(type, key)`` pair is this module's; the ``card`` is not — it is
    whatever the subject type's ``card_function`` answered, passed through
    untouched. Chat does not know what a listing is and never will, so it
    stores a name and asks the owner.

    Attributes:
        type: Registered subject type (``listing``, …).
        key: Opaque key within that type.
        card: The provider's card, or ``null`` when there is none to show.
            Its shape belongs to the provider — a deleted subject typically
            answers a ``gone`` card rather than nothing, so ``null`` here
            means the card could not be obtained, not that the subject is
            gone.
        meta_status: ``ok`` / ``partial`` / ``missing`` — the attachment
            vocabulary, for the same reason: a degraded header stays
            renderable and says why.
        meta_reason: ``subject_type_unregistered`` /
            ``card_function_unreachable`` / ``card_function_failed`` /
            ``card_missing``, or null when the status is ``ok``. A header that
            could not be built says WHY rather than looking like a thread
            about nothing.
    """

    type: str
    key: str
    card: Optional[Dict[str, Any]] = None
    meta_status: str = "ok"
    meta_reason: Optional[str] = None


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
        subject: What the thread is about, with its rendered card inlined —
            or ``null`` for a thread about nothing in particular, which is
            every thread a generic chat opens. Resolved in ONE batched call
            per subject type for a whole page, never one per conversation.
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
    subject: Optional[SubjectResponse] = None
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
        subject_type: What this thread is about, by registered type. Supply
            both subject fields or neither. **For a direct thread the subject
            is part of the thread's identity**: the same pair asking about a
            different subject get a different thread, where before 0.6.0 they
            were folded into the one thread they were allowed to have.
        subject_key: The opaque key within that type. Never parsed here.
        scope_key: Ignored — the scope is resolved server-side from the
            SCOPE_PROVIDER seam; present for symmetry only.
    """

    kind: str = "direct"
    participant_ids: List[str] = field(default_factory=list)
    subject_type: str = ""
    subject_key: str = ""
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
