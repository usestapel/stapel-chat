"""Models for stapel-chat.

The generic messaging core: ``Conversation`` (a thread — direct, group or
support), ``ConversationParticipant`` (membership + role + read marker) and
``Message`` (a monotonic per-conversation ``seq``, text or system body,
optional reply and opaque attachment keys).

House rules (docs/library-standard.md §3.8):
- the user model is only ``settings.AUTH_USER_MODEL``;
- **no FK to Organization/Workspace/CDN** — scoping is the opaque
  ``scope_key`` string (resolved via the SCOPE_PROVIDER seam) and file storage
  is the host's concern (a message carries only opaque attachment *keys*, never
  bytes);
- cross-service handles are UUIDs (``Conversation.id`` / ``Message.id``) so the
  ``chat.message`` emit carries stable ids the host can pin to.

Ordering is by ``Message.seq`` — a strictly monotonic counter allocated
per-conversation from ``Conversation.last_seq`` under a row lock. seq (not a
timestamp) is the canonical anchor for history pagination and the resume cursor
for realtime replay: it is gapless, total and never collides (the
``(conversation, seq)`` unique constraint is the backstop).
"""
import uuid

from django.conf import settings
from django.db import models


class ConversationKind(models.TextChoices):
    """The three flavors of thread, all backed by this one model.

    Members:
        DIRECT: A 1:1 thread, idempotent by its (scope, participant-pair) —
            a second create for the same pair returns the existing thread.
        GROUP: A many-party thread (no idempotency; each create is a new one).
        SUPPORT: A customer↔operator thread with a queue/assignment lifecycle
            (see ``SupportStatus`` and the support service layer).
    """

    DIRECT = "direct", "Direct"
    GROUP = "group", "Group"
    SUPPORT = "support", "Support"


class SupportStatus(models.TextChoices):
    """Lifecycle of a ``support`` conversation (blank for the other kinds).

    Members:
        OPEN: Awaiting or receiving attention (the queue state before assign,
            and the working state after — assignment does not change status,
            it sets the operator).
        PENDING: Parked waiting on the customer (operator's ball is not in
            their court).
        RESOLVED: Closed out. ``reopen`` flips it back to OPEN.
    """

    OPEN = "open", "Open"
    PENDING = "pending", "Pending"
    RESOLVED = "resolved", "Resolved"


class ParticipantRole(models.TextChoices):
    """A participant's role in a conversation.

    Members:
        MEMBER: An ordinary participant (both sides of a direct/group thread,
            and the customer in a support thread).
        OPERATOR: A support agent. Assigning a support conversation adds the
            agent as an ``operator`` participant.
    """

    MEMBER = "member", "Member"
    OPERATOR = "operator", "Operator"


class MessageKind(models.TextChoices):
    """A message's kind.

    Members:
        TEXT: An ordinary authored message (has a ``sender``).
        SYSTEM: A system/event line (assignment, resolve, …). ``sender`` is
            null; the body is a machine/i18n-friendly marker the host renders.
    """

    TEXT = "text", "Text"
    SYSTEM = "system", "System"


def _direct_key(scope_key: str, user_a, user_b) -> str:
    """Canonical dedup key for a direct thread: order-independent over the
    participant pair, namespaced by scope. Two users have exactly one direct
    thread per scope regardless of who initiates."""
    a, b = sorted((str(user_a), str(user_b)))
    return f"{scope_key}\x1f{a}\x1f{b}"


class Conversation(models.Model):
    """A thread of messages between participants.

    ``last_seq`` is the high-water mark of message ``seq`` in this thread; the
    send path locks the row, allocates ``last_seq + 1`` and stores it, so seq
    is gapless and monotonic even under concurrent sends.

    ``direct_key`` is set only for ``direct`` threads (the order-independent
    participant-pair key) and is uniquely constrained *among direct threads*,
    which is what makes direct creation idempotent. It is blank for group and
    support threads (which are never deduplicated).

    ``support_status`` is meaningful only for ``support`` threads (blank
    otherwise). ``assigned_operator`` is the currently assigned agent (null =
    unassigned, i.e. still in the queue).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=16, choices=ConversationKind.choices)
    # Opaque host-supplied scope (workspace_id / org_id / tenant / ""). The
    # library never interprets it; the SCOPE_PROVIDER seam resolves & filters.
    scope_key = models.CharField(max_length=255, blank=True, default="", db_index=True)

    # Idempotency key for direct threads only (blank elsewhere). See _direct_key.
    direct_key = models.CharField(max_length=600, blank=True, default="")

    # High-water mark of Message.seq in this thread (see class docstring).
    last_seq = models.PositiveBigIntegerField(default=0)

    # Support lifecycle (blank for direct/group).
    support_status = models.CharField(
        max_length=16, choices=SupportStatus.choices, blank=True, default=""
    )
    assigned_operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_conversations",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["scope_key", "kind"], name="chat_conv_scope_kind"),
            models.Index(
                fields=["kind", "support_status"], name="chat_conv_support_queue"
            ),
        ]
        constraints = [
            # At most one direct thread per (scope, participant-pair). Partial
            # so group/support threads (direct_key="") never collide.
            models.UniqueConstraint(
                fields=["direct_key"],
                condition=models.Q(kind="direct"),
                name="chat_conv_uniq_direct",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.id}"


class ConversationParticipant(models.Model):
    """Membership of a user in a conversation, with role and read marker.

    ``last_read_seq`` is the seq of the newest message this participant has
    read; unread for them is ``count(Message.seq > last_read_seq authored by
    someone else)``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="participants"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_participations",
    )
    role = models.CharField(
        max_length=16, choices=ParticipantRole.choices, default=ParticipantRole.MEMBER
    )
    last_read_seq = models.PositiveBigIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "user"], name="chat_participant_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["user"], name="chat_participant_user"),
        ]

    def __str__(self):
        return f"{self.user_id} @ {self.conversation_id} ({self.role})"


class Message(models.Model):
    """A message in a conversation.

    ``seq`` is the per-conversation monotonic order key (see
    :class:`Conversation`). ``sender`` is null for ``system`` messages.
    ``reply_to`` quotes an earlier message (nulled if that message is later
    erased). ``attachments`` is a list of opaque string keys pointing at the
    host's file storage — the module stores keys only, never bytes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chat_messages",
    )
    seq = models.PositiveBigIntegerField()
    kind = models.CharField(
        max_length=16, choices=MessageKind.choices, default=MessageKind.TEXT
    )
    body = models.TextField(blank=True, default="")
    reply_to = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="replies",
    )
    # Opaque attachment keys (e.g. "chat/<hash>") — files live in the host CDN.
    attachments = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["conversation", "seq"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "seq"], name="chat_message_uniq_seq"
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "seq"], name="chat_message_conv_seq"),
        ]

    def __str__(self):
        return f"{self.conversation_id}#{self.seq} ({self.kind})"
