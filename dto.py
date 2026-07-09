"""Dataclass DTOs — the API models of stapel-chat (never ORM instances)."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class ParticipantResponse:
    """A conversation participant.

    Attributes:
        user_id: The participant's user id.
        role: ``member`` or ``operator``.
        last_read_seq: seq of the newest message this participant has read.
    """

    user_id: str
    role: str
    last_read_seq: int


@dataclass
class MessageResponse:
    """A single message.

    Attributes:
        id: Message id (UUID).
        conversation_id: Owning conversation id.
        sender_id: Author's user id (null for system messages).
        seq: Monotonic per-conversation order key.
        kind: ``text`` or ``system``.
        body: Message text (may be empty when only attachments are present).
        reply_to: Quoted message id, if any.
        attachments: Opaque attachment keys (files live in the host CDN).
        created_at: Creation time (tz-aware ISO 8601).
    """

    id: str
    conversation_id: str
    seq: int
    kind: str
    body: str
    created_at: datetime
    sender_id: Optional[str] = None
    reply_to: Optional[str] = None
    attachments: List[str] = field(default_factory=list)


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

    Attributes:
        body: Message text (required unless attachments are supplied).
        attachments: Opaque attachment keys (host CDN references).
        reply_to: Message id being replied to (must be in this conversation).
    """

    body: str = ""
    attachments: List[str] = field(default_factory=list)
    reply_to: Optional[str] = None


@dataclass
class MarkReadRequest:
    """Advance the requesting user's read marker.

    Attributes:
        upto_seq: seq of the newest message now considered read.
    """

    upto_seq: int
