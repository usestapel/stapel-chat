"""DRF views for stapel-chat.

Thin views over :mod:`services`. Scope resolution/filtering goes through the
``SCOPE_PROVIDER`` seam so the host controls which conversations a request may
see and what ``scope_key`` a new conversation gets. History and conversation
lists are anchor-paginated (core ``AnchorPagination``): message history anchors
on ``seq`` — the canonical anchor case — and supports both directions.
"""
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.pagination import (
    AnchorPagination,
    CreatedAtAnchorPagination,
    UpdatedAtAnchorPagination,
)

# The serializer seam is the core's since 0.37.0 — twenty-four copies of the
# same two attributes and two getters was a missing primitive, not a pattern.
# The local copy this module used to carry is gone; the name is re-exported so
# a host that subclassed `stapel_chat.views.SerializerSeamMixin` keeps working.
from stapel_core.django.api.views import SerializerSeamMixin

from . import services
from .activity import UnknownActivityState
from .attachments import InvalidAttachment, UnknownAttachmentType
from .conf import chat_settings
from .dto import (
    AttachmentResponse,
    ConversationResponse,
    MessageResponse,
    ParticipantResponse,
)
from .errors import (
    ERR_400_ATTACHMENTS_DISABLED,
    ERR_400_BODY_TOO_LONG,
    ERR_400_EMPTY_MESSAGE,
    ERR_400_INVALID_ATTACHMENT,
    ERR_400_INVALID_DIRECT,
    ERR_400_INVALID_KIND,
    ERR_400_INVALID_REPLY,
    ERR_400_KIND_DISABLED,
    ERR_400_MESSAGE_DELETED,
    ERR_400_NOT_EDITABLE,
    ERR_400_UNKNOWN_ACTIVITY_STATE,
    ERR_400_UNKNOWN_ATTACHMENT_TYPE,
    ERR_403_NOT_AUTHOR,
    ERR_403_NOT_OPERATOR,
    ERR_403_NOT_PARTICIPANT,
    ERR_404_CONVERSATION_NOT_FOUND,
    ERR_404_MESSAGE_NOT_FOUND,
    ERR_409_ALREADY_ASSIGNED,
)
from .models import (
    Conversation,
    ConversationKind,
    Message,
    ParticipantRole,
)
from .realtime import conversation_stream
from .scope import get_scope_provider
from .serializers import (
    ActivityRequestSerializer,
    ConversationResponseSerializer,
    CreateConversationRequestSerializer,
    EditMessageRequestSerializer,
    MarkReadRequestSerializer,
    MessageResponseSerializer,
    SendMessageRequestSerializer,
)

_VALID_KINDS = {c.value for c in ConversationKind}

__all__ = [
    "SerializerSeamMixin",
    "ConversationListCreateView",
    "ConversationDetailView",
    "MessageListCreateView",
    "MessageDetailView",
    "MarkReadView",
    "ActivityView",
    "SupportQueueView",
    "SupportAssignView",
    "SupportResolveView",
    "SupportReopenView",
]


# ── Pagination ────────────────────────────────────────────────────────────


class MessageHistoryPagination(AnchorPagination):
    """History anchored on ``seq``, newest-first — the chat-natural default:
    open on the latest page and page ``direction=next`` to walk *older*
    messages (seq below the anchor), ``prev`` for newer, ``center`` around an
    anchor. seq is a gapless total order, so an anchored window is unaffected by
    messages that arrive after it was taken."""

    anchor_field = "seq"
    ordering = "-seq"
    page_size = 50
    max_page_size = 200


class ConversationListPagination(UpdatedAtAnchorPagination):
    page_size = 50
    max_page_size = 200


class SupportQueuePagination(CreatedAtAnchorPagination):
    # Oldest-waiting first — a FIFO queue.
    ordering = "created_at"
    page_size = 50
    max_page_size = 200


# ── Mappers ────────────────────────────────────────────────────────────────


def attachment_to_dto(raw: dict) -> AttachmentResponse:
    """One stored descriptor -> the response DTO.

    Only the declared fields are lifted into the DTO; a key the CDN added and
    this release has never heard of is simply not surfaced by REST (it still
    rides the wire frame, which is raw JSON). That is the deliberate asymmetry
    between a typed contract and a live socket.
    """
    if isinstance(raw, str):  # pre-0.3 rows before the data migration
        raw = {"key": raw, "type": "file"}
    return AttachmentResponse(
        key=raw.get("key") or "",
        type=raw.get("type") or "file",
        mime=raw.get("mime"),
        bytes=raw.get("bytes"),
        name=raw.get("name"),
        ext=raw.get("ext"),
        width=raw.get("width"),
        height=raw.get("height"),
        aspect=raw.get("aspect"),
        duration_ms=raw.get("duration_ms"),
        preview_b64=raw.get("preview_b64"),
        waveform_b64=raw.get("waveform_b64"),
        variants=list(raw.get("variants") or []),
    )


def message_to_dto(msg: Message) -> MessageResponse:
    """A message, or its tombstone — the deleted row is returned, not hidden.

    Filtering tombstones out of history would defeat their only purpose: a
    client cache learns which id to purge by seeing that id come back
    stripped, and an id that stops arriving is an id nobody can purge.
    """
    deleted = msg.deleted_at is not None
    return MessageResponse(
        id=str(msg.id),
        conversation_id=str(msg.conversation_id),
        seq=msg.seq,
        rev_seq=msg.rev_seq,
        kind=msg.kind,
        body="" if deleted else msg.body,
        created_at=msg.created_at,
        sender_id=str(msg.sender_id) if msg.sender_id else None,
        reply_to=str(msg.reply_to_id) if msg.reply_to_id else None,
        attachments=(
            [] if deleted else [attachment_to_dto(a) for a in (msg.attachments or [])]
        ),
        client_msg_id=msg.client_msg_id or None,
        edited=msg.edited_at is not None,
        edited_at=msg.edited_at,
        deleted=deleted,
        deleted_at=msg.deleted_at,
    )


def conversation_to_dto(conv: Conversation, viewer_participant=None) -> ConversationResponse:
    unread = (
        services.unread_count(conversation=conv, participant=viewer_participant)
        if viewer_participant is not None
        else 0
    )
    return ConversationResponse(
        id=str(conv.id),
        kind=conv.kind,
        scope_key=conv.scope_key,
        support_status=conv.support_status,
        last_seq=conv.last_seq,
        unread_count=unread,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        # Every conversation ships the address of its own live path. A client
        # never has to construct one, and a client that ignores it is visibly
        # ignoring a field rather than quietly falling back to a timer.
        stream_key=conversation_stream(conv.id),
        socket_path=f"ws/chat/{conv.id}",
        assigned_operator_id=(
            str(conv.assigned_operator_id) if conv.assigned_operator_id else None
        ),
        participants=[
            ParticipantResponse(
                user_id=str(p.user_id),
                role=p.role,
                last_read_seq=p.last_read_seq,
                last_delivered_seq=p.last_delivered_seq,
            )
            for p in conv.participants.all()
        ],
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _scoped(request):
    """All conversations in the request's scope (before participant scoping)."""
    return get_scope_provider().filter(Conversation.objects.all(), request)


def _get_conversation(request, conversation_id):
    return (
        _scoped(request)
        .prefetch_related("participants")
        .filter(id=conversation_id)
        .first()
    )


def _my_participant(conv, user):
    for p in conv.participants.all():
        if str(p.user_id) == str(user.id):
            return p
    return None


def _support_enabled() -> bool:
    return ConversationKind.SUPPORT in chat_settings.CHAT_KINDS


def _may_operate(request, conv=None) -> bool:
    """Operator authority, asked BEFORE the participant table is consulted.

    Every other support check reads ``ConversationParticipant`` — and assign
    writes that row, so a caller who reached assign had already answered its
    own question. This asks the seam instead. A provider that cannot find out
    raises (503); it never returns True on a failed lookup.
    """
    return get_scope_provider().can_operate(request, conv)


# ── Conversation views ─────────────────────────────────────────────────────


@extend_schema(tags=["Chat"])
class ConversationListCreateView(SerializerSeamMixin, APIView):
    """List the requesting user's conversations, or create one."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = CreateConversationRequestSerializer
    response_serializer_class = ConversationResponseSerializer
    pagination_class = ConversationListPagination

    @extend_schema(responses={200: ConversationResponseSerializer(many=True)})
    def get(self, request):  # noqa: R007
        qs = (
            _scoped(request)
            .filter(participants__user=request.user)
            .distinct()
            .prefetch_related("participants")
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request)
        response_cls = self.get_response_serializer_class()
        items = [
            response_cls(
                conversation_to_dto(c, _my_participant(c, request.user))
            ).data
            for c in page
        ]
        return paginator.get_paginated_response(items)

    @extend_schema(
        request=CreateConversationRequestSerializer,
        responses={201: ConversationResponseSerializer},
    )
    def post(self, request):  # noqa: R007
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        kind = data.kind
        if kind not in _VALID_KINDS:
            return StapelErrorResponse(400, ERR_400_INVALID_KIND)
        if kind not in chat_settings.CHAT_KINDS:
            return StapelErrorResponse(400, ERR_400_KIND_DISABLED)
        scope_key = get_scope_provider().resolve(request)
        participant_ids = data.participant_ids or []

        if kind == ConversationKind.DIRECT:
            others = [pid for pid in participant_ids if str(pid) != str(request.user.id)]
            if len(others) != 1:
                return StapelErrorResponse(400, ERR_400_INVALID_DIRECT)
            conv = services.create_direct(
                owner=request.user, other_user_id=others[0], scope_key=scope_key
            )
        elif kind == ConversationKind.GROUP:
            conv = services.create_group(
                owner=request.user, participant_ids=participant_ids, scope_key=scope_key
            )
        else:  # support
            conv = services.create_support(
                customer=request.user, scope_key=scope_key
            )

        conv = (
            Conversation.objects.prefetch_related("participants")
            .filter(id=conv.id)
            .first()
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(conversation_to_dto(conv, _my_participant(conv, request.user))),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Chat"])
class ConversationDetailView(SerializerSeamMixin, APIView):
    """Retrieve a single conversation (participant-only)."""

    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = ConversationResponseSerializer

    @extend_schema(responses={200: ConversationResponseSerializer})
    def get(self, request, conversation_id):  # noqa: R007
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        participant = _my_participant(conv, request.user)
        if participant is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(conversation_to_dto(conv, participant)))


@extend_schema(tags=["Chat"])
class MessageListCreateView(SerializerSeamMixin, APIView):
    """History (anchor by seq, both directions) or send a message."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = SendMessageRequestSerializer
    response_serializer_class = MessageResponseSerializer
    pagination_class = MessageHistoryPagination

    @extend_schema(responses={200: MessageResponseSerializer(many=True)})
    def get(self, request, conversation_id):  # noqa: R007
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        if _my_participant(conv, request.user) is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        qs = Message.objects.filter(conversation=conv).select_related("sender")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request)
        response_cls = self.get_response_serializer_class()
        items = [response_cls(message_to_dto(m)).data for m in page]
        return paginator.get_paginated_response(items)

    @extend_schema(
        request=SendMessageRequestSerializer,
        responses={201: MessageResponseSerializer},
    )
    def post(self, request, conversation_id):  # noqa: R007
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        if _my_participant(conv, request.user) is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        body = data.body or ""
        attachments = data.attachments or []
        if not body.strip() and not attachments:
            return StapelErrorResponse(400, ERR_400_EMPTY_MESSAGE)
        if attachments and not chat_settings.ATTACHMENTS:
            return StapelErrorResponse(400, ERR_400_ATTACHMENTS_DISABLED)
        if len(body) > chat_settings.MAX_BODY_LENGTH:
            return StapelErrorResponse(400, ERR_400_BODY_TOO_LONG)
        reply_to = None
        if data.reply_to:
            reply_to = Message.objects.filter(
                pk=data.reply_to, conversation=conv
            ).first()
            if reply_to is None:
                return StapelErrorResponse(400, ERR_400_INVALID_REPLY)
        try:
            msg = services.post_message(
                conversation=conv,
                sender=request.user,
                body=body,
                attachments=list(attachments),
                reply_to=reply_to,
                client_msg_id=data.client_msg_id or "",
            )
        except services.InvalidReply:
            return StapelErrorResponse(400, ERR_400_INVALID_REPLY)
        except UnknownAttachmentType:
            return StapelErrorResponse(400, ERR_400_UNKNOWN_ATTACHMENT_TYPE)
        except InvalidAttachment:
            return StapelErrorResponse(400, ERR_400_INVALID_ATTACHMENT)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(message_to_dto(msg)), status=status.HTTP_201_CREATED
        )


@extend_schema(tags=["Chat"])
class MessageDetailView(SerializerSeamMixin, APIView):
    """Edit or delete one message. Author only.

    The HTTP twin of the socket's ``edit`` / ``delete`` frames — same service
    calls, same emits, same fan-out. It exists so a client that has just
    rehydrated over REST is not obliged to open a socket to correct a typo,
    not because REST is the intended path: the socket is.

    ``DELETE`` leaves a **tombstone** and answers ``200`` with the stripped
    message, not ``204``. The body is the point — the caller (and every other
    subscriber, over the socket) is handed the exact row shape that says "this
    id is now empty", which is what a local cache purges against.
    """

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = EditMessageRequestSerializer
    response_serializer_class = MessageResponseSerializer

    def _load(self, request, conversation_id, message_id):
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return None, None, StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        if _my_participant(conv, request.user) is None:
            return None, None, StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        msg = Message.objects.filter(pk=message_id, conversation=conv).first()
        if msg is None:
            return None, None, StapelErrorResponse(404, ERR_404_MESSAGE_NOT_FOUND)
        return conv, msg, None

    @extend_schema(
        request=EditMessageRequestSerializer,
        responses={200: MessageResponseSerializer},
    )
    def patch(self, request, conversation_id, message_id):  # noqa: R007
        _, msg, err = self._load(request, conversation_id, message_id)
        if err is not None:
            return err
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        body = (ser.validated_data.body or "").strip()
        if not body:
            return StapelErrorResponse(400, ERR_400_EMPTY_MESSAGE)
        if len(body) > chat_settings.MAX_BODY_LENGTH:
            return StapelErrorResponse(400, ERR_400_BODY_TOO_LONG)
        try:
            msg = services.edit_message(message=msg, editor=request.user, body=body)
        except services.NotAuthor:
            return StapelErrorResponse(403, ERR_403_NOT_AUTHOR)
        except services.MessageGone:
            return StapelErrorResponse(400, ERR_400_MESSAGE_DELETED)
        except services.NotEditable:
            return StapelErrorResponse(400, ERR_400_NOT_EDITABLE)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(message_to_dto(msg)))

    @extend_schema(request=None, responses={200: MessageResponseSerializer})
    def delete(self, request, conversation_id, message_id):  # noqa: R007
        _, msg, err = self._load(request, conversation_id, message_id)
        if err is not None:
            return err
        try:
            msg = services.delete_message(message=msg, actor=request.user)
        except services.NotAuthor:
            return StapelErrorResponse(403, ERR_403_NOT_AUTHOR)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(response_cls(message_to_dto(msg)))


@extend_schema(tags=["Chat"])
class MarkReadView(SerializerSeamMixin, APIView):
    """Advance the requesting user's read and delivery markers.

    Both markers are durable rows and both fan out a live receipt when they
    move — ``chat.read`` / ``chat.delivered`` on the conversation stream. The
    receipt is a Signal, not an event: the truth is on the participant row and
    comes back with the conversation, so nobody who was offline is owed a
    replay of a tick mark.
    """

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = MarkReadRequestSerializer

    @extend_schema(request=MarkReadRequestSerializer, responses={200: None})
    def post(self, request, conversation_id):  # noqa: R007
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        if _my_participant(conv, request.user) is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        moved = services.mark_read(
            conversation=conv, user=request.user, upto_seq=data.upto_seq
        )
        delivered = False
        # A read implies delivery — you cannot read what you do not hold — so
        # the delivery marker never lags behind the read one.
        delivered_upto = max(int(data.delivered_upto_seq or 0), int(data.upto_seq or 0))
        if delivered_upto:
            delivered = services.mark_delivered(
                conversation=conv, user=request.user, upto_seq=delivered_upto
            )
        return StapelResponse({"updated": moved, "delivered": delivered})  # noqa: R006


@extend_schema(tags=["Chat"])
class ActivityView(SerializerSeamMixin, APIView):
    """Announce "typing…" (or any registered activity) to the conversation.

    Nothing is stored and nothing is returned but the resolved TTL. The
    endpoint exists for parity — a client whose socket is momentarily down can
    still say it is typing — and it is explicitly *not* the intended path:
    an activity state is worth less than the round trip that carries it, which
    is why the socket's ``activity`` frame is the one a UI should use.
    """

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = ActivityRequestSerializer

    @extend_schema(request=ActivityRequestSerializer, responses={200: None})
    def post(self, request, conversation_id):  # noqa: R007
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        if _my_participant(conv, request.user) is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            resolved = services.announce_activity(
                conversation=conv, user=request.user, state=ser.validated_data.state
            )
        except UnknownActivityState:
            return StapelErrorResponse(400, ERR_400_UNKNOWN_ACTIVITY_STATE)
        return StapelResponse(resolved)  # noqa: R006


# ── Support views ──────────────────────────────────────────────────────────


@extend_schema(tags=["Chat support"])
class SupportQueueView(SerializerSeamMixin, APIView):
    """The queue of unassigned, still-active support conversations."""

    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = ConversationResponseSerializer
    pagination_class = SupportQueuePagination

    @extend_schema(responses={200: ConversationResponseSerializer(many=True)})
    def get(self, request):  # noqa: R007
        if not _support_enabled():
            return StapelErrorResponse(400, ERR_400_KIND_DISABLED)
        # The rows carry scope_key and every participant's user_id: the queue
        # is an operator surface, not a listing.
        if not _may_operate(request):
            return StapelErrorResponse(403, ERR_403_NOT_OPERATOR)
        qs = services.support_queue(qs=_scoped(request)).prefetch_related(
            "participants"
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request)
        response_cls = self.get_response_serializer_class()
        items = [
            response_cls(
                conversation_to_dto(c, _my_participant(c, request.user))
            ).data
            for c in page
        ]
        return paginator.get_paginated_response(items)


@extend_schema(tags=["Chat support"])
class SupportAssignView(SerializerSeamMixin, APIView):
    """Assign the requesting user (as operator) to a support conversation."""

    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = ConversationResponseSerializer

    @extend_schema(request=None, responses={200: ConversationResponseSerializer})
    def post(self, request, conversation_id):  # noqa: R007
        if not _support_enabled():
            return StapelErrorResponse(400, ERR_400_KIND_DISABLED)
        conv = _get_conversation(request, conversation_id)
        if conv is None or conv.kind != ConversationKind.SUPPORT:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        # Before the write, not after: assigning MINTS the operator-role
        # participant row that every later check on this thread trusts.
        if not _may_operate(request, conv):
            return StapelErrorResponse(403, ERR_403_NOT_OPERATOR)
        try:
            conv = services.assign_operator(conversation=conv, operator=request.user)
        except services.AlreadyAssigned:
            return StapelErrorResponse(409, ERR_409_ALREADY_ASSIGNED)
        return self._reload(conv, request)

    def _reload(self, conv, request):
        conv = (
            Conversation.objects.prefetch_related("participants")
            .filter(id=conv.id)
            .first()
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(conversation_to_dto(conv, _my_participant(conv, request.user)))
        )


class _SupportTransitionView(SerializerSeamMixin, APIView):
    """Shared base for resolve/reopen (operator-only)."""

    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = ConversationResponseSerializer

    def _transition(self, conv):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request, conversation_id):
        if not _support_enabled():
            return StapelErrorResponse(400, ERR_400_KIND_DISABLED)
        conv = _get_conversation(request, conversation_id)
        if conv is None or conv.kind != ConversationKind.SUPPORT:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        participant = _my_participant(conv, request.user)
        if participant is None or participant.role != ParticipantRole.OPERATOR:
            return StapelErrorResponse(403, ERR_403_NOT_OPERATOR)
        # The participant row above is necessary, never sufficient: it is
        # writable by the very endpoint this pair follows.
        if not _may_operate(request, conv):
            return StapelErrorResponse(403, ERR_403_NOT_OPERATOR)
        conv = self._transition(conv)
        conv = (
            Conversation.objects.prefetch_related("participants")
            .filter(id=conv.id)
            .first()
        )
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(conversation_to_dto(conv, _my_participant(conv, request.user)))
        )


@extend_schema(tags=["Chat support"], request=None, responses={200: ConversationResponseSerializer})
class SupportResolveView(_SupportTransitionView):
    """Mark a support conversation resolved."""

    def _transition(self, conv):
        return services.resolve_support(conversation=conv)


@extend_schema(tags=["Chat support"], request=None, responses={200: ConversationResponseSerializer})
class SupportReopenView(_SupportTransitionView):
    """Reopen a resolved support conversation."""

    def _transition(self, conv):
        return services.reopen_support(conversation=conv)
