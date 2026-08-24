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

    ``last_delivered_seq`` is the newest message their *client* acknowledged
    receiving. Delivered and read are two different facts — a phone with the
    app in the background has the message, and nobody has looked at it — and a
    UI that draws one tick and two ticks needs both. Both markers only ever
    move forward, and both are readable over REST, which is what lets the
    live receipt travel as an ephemeral Signal instead of a durable event.
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
    last_delivered_seq = models.PositiveBigIntegerField(default=0)

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
    erased). ``attachments`` is a list of render descriptors, each carrying an
    opaque CDN ``key`` plus the metadata a bubble needs to paint without a
    second round trip — the module stores descriptors only, never bytes (see
    :mod:`stapel_chat.attachments`).

    Two sequences, and confusing them is the bug this docstring exists to
    prevent:

    * ``seq`` — the message's **position in the thread**. Allocated once, at
      creation, and never touched again. History pagination anchors on it and
      a client sorts by it.
    * ``rev_seq`` — the message's position in the conversation's **journal of
      revisions**. It starts equal to ``seq`` and is re-allocated (to a fresh
      ``last_seq + 1``) every time the row changes: an edit, a delete. It is
      the resume cursor: a socket replaying ``rev_seq > last_seq`` receives
      every message whose *content* the client has not seen, including one
      posted long ago and edited a minute back. Without it, an edit made while
      a client was offline would be invisible forever — the row would carry a
      seq the client had already acknowledged.

    A client therefore **upserts by ``id``, orders by ``seq``**, and treats the
    frame's own sequence purely as a cursor.

    **Deletion is a tombstone, never a removal.** ``deleted_at`` is stamped,
    ``body`` is emptied and ``attachments`` is cleared, and the row keeps being
    delivered — with a fresh ``rev_seq`` — precisely so that every client cache
    and offline database learns which id to purge. A row that simply vanished
    would leave the copy on the client forever, which is the opposite of what
    a delete is for. Retention: **permanent**. See MODULE.md for why there is
    no TTL.
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
    # Journal position of the newest revision of this row (== seq until the
    # first edit/delete). The resume cursor — see the class docstring.
    rev_seq = models.PositiveBigIntegerField(default=0, db_index=True)
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
    # Render descriptors: [{key, type, mime, bytes, aspect, preview_b64, ...}].
    # Files live in the host CDN; this module never sees bytes.
    attachments = models.JSONField(default=list, blank=True)

    # Sender-generated idempotency key. A send retried over a socket that
    # dropped mid-flight must not post the message twice — the client keeps
    # the same id and the second attempt returns the first row. It also lets
    # the sender reconcile its optimistic bubble with the frame that comes
    # back, which is what makes Enter-to-send feel instant without any
    # server-side draft concept.
    client_msg_id = models.CharField(max_length=64, blank=True, default="")

    # Presence is the flag. `edited_at` null == never edited; `deleted_at`
    # null == live. Two timestamps rather than two booleans + two timestamps:
    # a boolean that can disagree with its timestamp is a bug waiting.
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["conversation", "seq"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "seq"], name="chat_message_uniq_seq"
            ),
            # Idempotent send. Partial so the blank default (system lines, a
            # client that sends none) never collides.
            models.UniqueConstraint(
                fields=["conversation", "client_msg_id"],
                condition=~models.Q(client_msg_id=""),
                name="chat_message_uniq_client_id",
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "seq"], name="chat_message_conv_seq"),
            # The replay query: rev_seq > cursor within one conversation.
            models.Index(
                fields=["conversation", "rev_seq"], name="chat_message_conv_rev"
            ),
        ]

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_edited(self) -> bool:
        return self.edited_at is not None

    def __str__(self):
        state = " [deleted]" if self.deleted_at else ""
        return f"{self.conversation_id}#{self.seq} ({self.kind}){state}"
