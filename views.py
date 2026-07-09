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

from . import services
from .conf import chat_settings
from .dto import ConversationResponse, MessageResponse, ParticipantResponse
from .errors import (
    ERR_400_ATTACHMENTS_DISABLED,
    ERR_400_BODY_TOO_LONG,
    ERR_400_EMPTY_MESSAGE,
    ERR_400_INVALID_DIRECT,
    ERR_400_INVALID_KIND,
    ERR_400_INVALID_REPLY,
    ERR_400_KIND_DISABLED,
    ERR_403_NOT_OPERATOR,
    ERR_403_NOT_PARTICIPANT,
    ERR_404_CONVERSATION_NOT_FOUND,
    ERR_409_ALREADY_ASSIGNED,
)
from .models import (
    Conversation,
    ConversationKind,
    Message,
    ParticipantRole,
)
from .scope import get_scope_provider
from .serializers import (
    ConversationResponseSerializer,
    CreateConversationRequestSerializer,
    MarkReadRequestSerializer,
    MessageResponseSerializer,
    SendMessageRequestSerializer,
)

_VALID_KINDS = {c.value for c in ConversationKind}


class SerializerSeamMixin:
    """Overridable serializer seam for every stapel-chat APIView.

    Host projects can swap the request/response serializer of any view by
    subclassing and setting ``request_serializer_class`` /
    ``response_serializer_class`` — no need to rewrite the HTTP method bodies.
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


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


def message_to_dto(msg: Message) -> MessageResponse:
    return MessageResponse(
        id=str(msg.id),
        conversation_id=str(msg.conversation_id),
        seq=msg.seq,
        kind=msg.kind,
        body=msg.body,
        created_at=msg.created_at,
        sender_id=str(msg.sender_id) if msg.sender_id else None,
        reply_to=str(msg.reply_to_id) if msg.reply_to_id else None,
        attachments=list(msg.attachments or []),
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
        assigned_operator_id=(
            str(conv.assigned_operator_id) if conv.assigned_operator_id else None
        ),
        participants=[
            ParticipantResponse(
                user_id=str(p.user_id), role=p.role, last_read_seq=p.last_read_seq
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


# ── Conversation views ─────────────────────────────────────────────────────


@extend_schema(tags=["Chat"])
class ConversationListCreateView(SerializerSeamMixin, APIView):
    """List the requesting user's conversations, or create one."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = CreateConversationRequestSerializer
    response_serializer_class = ConversationResponseSerializer
    pagination_class = ConversationListPagination

    @extend_schema(responses={200: ConversationResponseSerializer(many=True)})
    def get(self, request):
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
    def post(self, request):
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
    def get(self, request, conversation_id):
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
    def get(self, request, conversation_id):
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
    def post(self, request, conversation_id):
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
            )
        except services.InvalidReply:
            return StapelErrorResponse(400, ERR_400_INVALID_REPLY)
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(message_to_dto(msg)), status=status.HTTP_201_CREATED
        )


@extend_schema(tags=["Chat"])
class MarkReadView(SerializerSeamMixin, APIView):
    """Advance the requesting user's read marker in a conversation."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = MarkReadRequestSerializer

    @extend_schema(request=MarkReadRequestSerializer, responses={200: None})
    def post(self, request, conversation_id):
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        if _my_participant(conv, request.user) is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        moved = services.mark_read(
            conversation=conv, user=request.user, upto_seq=ser.validated_data.upto_seq
        )
        return StapelResponse({"updated": moved})


# ── Support views ──────────────────────────────────────────────────────────


@extend_schema(tags=["Chat support"])
class SupportQueueView(SerializerSeamMixin, APIView):
    """The queue of unassigned, still-active support conversations."""

    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = ConversationResponseSerializer
    pagination_class = SupportQueuePagination

    @extend_schema(responses={200: ConversationResponseSerializer(many=True)})
    def get(self, request):
        if not _support_enabled():
            return StapelErrorResponse(400, ERR_400_KIND_DISABLED)
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
    def post(self, request, conversation_id):
        if not _support_enabled():
            return StapelErrorResponse(400, ERR_400_KIND_DISABLED)
        conv = _get_conversation(request, conversation_id)
        if conv is None or conv.kind != ConversationKind.SUPPORT:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
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
