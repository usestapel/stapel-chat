"""DRF views for stapel-chat.

Thin views over :mod:`services`. Scope resolution/filtering goes through the
``SCOPE_PROVIDER`` seam so the host controls which conversations a request may
see and what ``scope_key`` a new conversation gets. History and conversation
lists are anchor-paginated (core ``AnchorPagination``): message history anchors
on ``seq`` — the canonical anchor case — and supports both directions.
"""
from drf_spectacular.utils import OpenApiParameter, extend_schema
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

from . import services, subjects
from .activity import UnknownActivityState
from .attachments import InvalidAttachment, UnknownAttachmentType
from .conf import chat_settings
from .dto import (
    AttachmentResponse,
    ConversationResponse,
    LastMessageResponse,
    MessageResponse,
    ParticipantResponse,
    SubjectResponse,
)
from .errors import (
    ERR_400_ATTACHMENTS_DISABLED,
    ERR_400_BODY_TOO_LONG,
    ERR_400_EMPTY_MESSAGE,
    ERR_400_INVALID_ATTACHMENT,
    ERR_400_INVALID_DIRECT,
    ERR_400_INCOMPLETE_SUBJECT,
    ERR_400_INVALID_KIND,
    ERR_400_INVALID_REPLY,
    ERR_400_KIND_DISABLED,
    ERR_400_MESSAGE_DELETED,
    ERR_400_NOT_EDITABLE,
    ERR_400_UNKNOWN_ACTIVITY_STATE,
    ERR_400_UNKNOWN_ATTACHMENT_TYPE,
    ERR_400_UNKNOWN_SUBJECT_TYPE,
    ERR_403_NOT_AUTHOR,
    ERR_403_NOT_OPERATOR,
    ERR_403_NOT_PARTICIPANT,
    ERR_404_CONVERSATION_NOT_FOUND,
    ERR_404_MESSAGE_NOT_FOUND,
    ERR_403_SEND_REFUSED,
    ERR_409_ALREADY_ASSIGNED,
    ERR_503_BLOCKS_UNAVAILABLE,
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
    "RejoinConversationView",
    "ClearConversationView",
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


class LeftConversationListPagination(AnchorPagination):
    """``?left=true`` anchored on WHEN THE CALLER LEFT, newest departure first.

    A different anchor field from the default list, and deliberately so: the
    one thing a person scanning threads they walked out of is looking for is
    the one they walked out of last. ``viewer_left_at`` is the caller's own
    stamp, annotated per page by :func:`services.left_of`, never the joined
    participant column — see the note there.
    """

    anchor_field = "viewer_left_at"
    ordering = "-viewer_left_at"
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
        square=raw.get("square"),
        animated=raw.get("animated"),
        duration_ms=raw.get("duration_ms"),
        preview_b64=raw.get("preview_b64"),
        preview_kind=raw.get("preview_kind"),
        poster_url=raw.get("poster_url"),
        meta_status=raw.get("meta_status") or "missing",
        meta_reason=raw.get("meta_reason"),
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


def subject_to_dto(conv: Conversation, resolution=None) -> SubjectResponse | None:
    """The conversation's subject, with whatever card was resolved for it.

    ``resolution`` comes from the batched lookup; ``None`` means nobody asked
    for a card (a single-conversation read that chose not to), which is
    reported as a degraded card rather than as "no subject" — the thread does
    have one, and a header that silently vanishes is how the wrong card gets
    rendered instead of no card.
    """
    if not conv.subject_type or not conv.subject_key:
        return None
    resolution = resolution or {
        "card": None,
        "meta_status": subjects.META_PARTIAL,
        "meta_reason": subjects.REASON_UNREACHABLE,
    }
    return SubjectResponse(
        type=conv.subject_type,
        key=conv.subject_key,
        card=resolution.get("card"),
        meta_status=resolution.get("meta_status") or subjects.META_PARTIAL,
        meta_reason=resolution.get("meta_reason"),
    )


def last_message_to_dto(
    conv: Conversation, viewer_participant=None
) -> LastMessageResponse | None:
    """The line the row draws under the title — from the page's annotation.

    A list annotates the whole page in its own query
    (:func:`services.with_last_message`); a caller that hands over a
    conversation nobody annotated pays the one read it needs
    (:func:`services.last_message_of`), exactly as the unread count does. What
    is NOT allowed is a preview computed here from a rule of its own: the text
    a row draws is :func:`services.drawn_last_line`, the same rule
    ``?search=`` matches on, and a second copy of it would let the search find
    a row whose preview says something else.

    Both paths are bounded by the viewer's own cleared mark: the annotated one
    by the page's query (``services.with_last_message(viewer=…)``) and the
    fallback by ``viewer_participant.cleared_at``, which the caller is already
    holding — so a row whose history this person cleared draws the same blank
    line a thread nobody has written in draws, whichever path produced it.
    """
    if hasattr(conv, "last_message_seq"):  # annotated: a page, or a detail read
        seq = conv.last_message_seq
        if seq is None:  # annotated and empty — a thread with no messages
            return None
        kind = conv.last_message_kind
        body = conv.last_message_body
        deleted_at = conv.last_message_deleted_at
        sender_id = conv.last_message_sender_id
        created_at = conv.last_message_created_at
        attachments = conv.last_message_attachments
    else:
        msg = services.last_message_of(
            conv,
            cleared_at=(
                viewer_participant.cleared_at if viewer_participant is not None else None
            ),
        )
        if msg is None:
            return None
        seq, kind, body = msg.seq, msg.kind, msg.body
        deleted_at, sender_id, created_at = (
            msg.deleted_at,
            msg.sender_id,
            msg.created_at,
        )
        attachments = msg.attachments
    deleted = deleted_at is not None
    return LastMessageResponse(
        seq=int(seq),
        kind=kind,
        sender_id=str(sender_id) if sender_id else None,
        created_at=created_at,
        body_preview=services.preview_of(
            services.drawn_last_line(kind=kind, body=body or "", deleted=deleted)
        ),
        preview_reason=services.last_line_reason(
            kind=kind,
            body=body or "",
            deleted=deleted,
            has_attachments=bool(attachments),
        ),
    )


def conversation_to_dto(
    conv: Conversation,
    viewer_participant=None,
    subject_resolution=None,
    presence=None,
) -> ConversationResponse:
    # A page resolves presence once for every participant on it and passes the
    # map in; a caller that passes none simply ships the offline default,
    # which is the honest degradation (never a fabricated "online").
    presence = presence or {}
    # A list annotates the count for the whole page (services.with_viewer_unread)
    # — reading it per row was one query per conversation, which is the shape a
    # fifty-row inbox must never have. A single-conversation read is handed no
    # annotation and pays the one count it needs.
    annotated = getattr(conv, "viewer_unread", None)
    if annotated is not None:
        unread = int(annotated)
    else:
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
        subject=subject_to_dto(conv, subject_resolution),
        last_message=last_message_to_dto(conv, viewer_participant),
        participants=[
            ParticipantResponse(
                user_id=str(p.user_id),
                role=p.role,
                last_read_seq=p.last_read_seq,
                last_delivered_seq=p.last_delivered_seq,
                online=bool(
                    (presence.get(str(p.user_id)) or {}).get("online", False)
                ),
                last_seen_at=(presence.get(str(p.user_id)) or {}).get(
                    "last_seen_at"
                ),
                online_until=(presence.get(str(p.user_id)) or {}).get(
                    "online_until"
                ),
                left_at=p.left_at,
            )
            for p in conv.participants.all()
        ],
        # The caller's own departure, lifted out of `participants` because it
        # is what the ROW is rendered from — read off the participant row, not
        # off `viewer_left_at`, so a detail read and every listing answer with
        # the same field rather than the `?left=true` page alone.
        left_at=viewer_participant.left_at if viewer_participant is not None else None,
        # The caller's own mark, and only ever the caller's — see the field's
        # docstring for why it is not on `participants` beside `left_at`.
        cleared_at=(
            viewer_participant.cleared_at if viewer_participant is not None else None
        ),
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _scoped(request):
    """All conversations in the request's scope (before participant scoping)."""
    return get_scope_provider().filter(Conversation.objects.all(), request)


def _get_conversation(request, conversation_id):
    # The last line rides along in the same SELECT here too: a single read
    # already knows its own seq, and annotating costs nothing where the
    # per-row fallback in `last_message_to_dto` would cost a query. Annotated
    # FOR the requesting user, so a thread they cleared draws a blank line
    # here exactly as it does on their list.
    return (
        services.with_last_message(_scoped(request), viewer=request.user)
        .prefetch_related("participants")
        .filter(id=conversation_id)
        .first()
    )


def _my_participant(conv, user):
    for p in conv.participants.all():
        if str(p.user_id) == str(user.id):
            return p
    return None


#: Query-string spellings of "yes". A flag is on when it says so; anything else
#: (including "false", "0" and a typo) leaves the filter off, because a list
#: endpoint that 400s on a stray query parameter breaks every client that adds
#: one it knows nothing about.
_TRUTHY = {"true", "1", "yes", "on"}


def _flag(request, name: str) -> bool:
    """A boolean query parameter, read leniently — see :data:`_TRUTHY`."""
    return str(request.query_params.get(name, "")).strip().lower() in _TRUTHY


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

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                description=(
                    "Case-insensitive substring over the three things an inbox "
                    "row draws: the COUNTERPART'S DISPLAY NAME (the user-model "
                    "fields STAPEL_CHAT['SEARCH_NAME_FIELDS'] names — username, "
                    "first name and last name out of the box), the SUBJECT "
                    "CARD'S TITLE where the thread carries a subject (the "
                    "fields that subject type's `search_fields` policy names, "
                    "`title` by default), and the LAST MESSAGE'S body — the "
                    "very text that row's `last_message.body_preview` ships, "
                    "one rule for both. A tombstone is never matched (it draws "
                    "as deleted), and neither is a system marker, unless this "
                    "deployment gave that marker words in "
                    "STAPEL_CHAT['SYSTEM_LINE_LABELS'] — then the row draws "
                    "the label and those words find it. Filters BEFORE paging: anchor, "
                    "direction and limit walk the filtered list and mean "
                    "exactly what they mean without a search. Blank or "
                    "whitespace-only is no search at all. Title matching covers "
                    "the newest STAPEL_CHAT['SEARCH_SUBJECT_SCAN'] subject "
                    "threads (500 by default); older ones are still matched by "
                    "name and last line."
                ),
            ),
            OpenApiParameter(
                name="unread",
                type=bool,
                location=OpenApiParameter.QUERY,
                description=(
                    "`true` returns only conversations whose `unread_count` is "
                    "above zero for the caller — the same rule that produces "
                    "the number on each row (messages past your read marker, "
                    "written by somebody else, tombstones and system lines "
                    "excluded). Any other value is no filter. Composes with "
                    "`search` (both narrow, then the page is taken)."
                ),
            ),
            OpenApiParameter(
                name="left",
                type=bool,
                location=OpenApiParameter.QUERY,
                description=(
                    "`true` returns ONLY the threads the caller has LEFT — the "
                    "exact complement of the default list, never a widening of "
                    "it, so a thread is on one of the two and never on both. "
                    "It exists because leaving destroys nothing: without a "
                    "listing that shows them, a thread left by mistake is "
                    "reachable only by a URL somebody kept. Ordered by WHEN "
                    "THE CALLER LEFT, newest departure first — so `anchor` is "
                    "that timestamp on this list, not `updated_at` — and every "
                    "row carries it as `left_at`. `search` and `unread` "
                    "compose exactly as they do on the default list. Any other "
                    "value is the default list, unchanged. `POST "
                    "/conversations/{id}/rejoin` is the way back."
                ),
            ),
        ],
        responses={200: ConversationResponseSerializer(many=True)},
    )
    def get(self, request):  # noqa: R007
        # Whose list this is — a party to the thread who has not left it. The
        # rule lives in the service (`services.inbox_of`) because the badge,
        # the `unread=true` chip and this list must never disagree about which
        # threads are on it. `?left=true` asks for its exact complement
        # (`services.left_of`), which is the only listing a left thread is on.
        left_only = _flag(request, "left")
        base = _scoped(request)
        qs = (
            services.left_of(base, viewer=request.user)
            if left_only
            else services.inbox_of(base, viewer=request.user)
        ).prefetch_related("participants")
        # The unread count for the WHOLE page in two subqueries, which is also
        # what `unread=true` filters on — one rule, so the filter and the badge
        # can never disagree.
        qs = services.with_viewer_unread(qs, viewer=request.user)
        # …and the line every row draws under the title, in the same SELECT.
        # Without it a client has one request per row to paint an inbox, which
        # is the shape `GET /messages?limit=1` per conversation would have.
        qs = services.with_last_message(qs, viewer=request.user)
        qs, resolved_subjects = services.filter_inbox(
            qs,
            viewer=request.user,
            search=request.query_params.get("search") or "",
            unread_only=_flag(request, "unread"),
        )
        # Same paging primitive, a different anchor: a left list is walked by
        # the departure, an inbox by the thread's own last activity.
        paginator = (
            LeftConversationListPagination()
            if left_only
            else self.pagination_class()
        )
        page = paginator.paginate_queryset(qs, request)
        # ONE card call per subject type for the whole page. Resolving per
        # conversation would make a fifty-row inbox fifty round trips, which
        # is why the provider contract is a batch in the first place. A search
        # that already resolved these cards hands them over rather than making
        # the same provider answer the same keys twice in one request.
        cards = services.subject_cards_for(page, resolved=resolved_subjects)
        # Same batching rule for presence: one query for every participant on
        # the page, never one per row.
        presence = services.presence_for(page, viewer=request.user)
        response_cls = self.get_response_serializer_class()
        items = [
            response_cls(
                conversation_to_dto(
                    c, _my_participant(c, request.user), cards.get(str(c.id)), presence
                )
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

        subject_type = (data.subject_type or "").strip()
        subject_key = (data.subject_key or "").strip()

        try:
            if kind == ConversationKind.DIRECT:
                others = [
                    pid for pid in participant_ids if str(pid) != str(request.user.id)
                ]
                if len(others) != 1:
                    return StapelErrorResponse(400, ERR_400_INVALID_DIRECT)
                conv = services.create_direct(
                    owner=request.user,
                    other_user_id=others[0],
                    scope_key=scope_key,
                    subject_type=subject_type,
                    subject_key=subject_key,
                )
            elif kind == ConversationKind.GROUP:
                conv = services.create_group(
                    owner=request.user,
                    participant_ids=participant_ids,
                    scope_key=scope_key,
                    subject_type=subject_type,
                    subject_key=subject_key,
                )
            else:  # support
                # A support thread is about the deployment, not about an
                # object in it; a subject here would be a field nobody reads.
                conv = services.create_support(
                    customer=request.user, scope_key=scope_key
                )
        except services.UnknownSubjectType:
            return StapelErrorResponse(400, ERR_400_UNKNOWN_SUBJECT_TYPE)
        except services.IncompleteSubject:
            return StapelErrorResponse(400, ERR_400_INCOMPLETE_SUBJECT)
        except services.SendRefused:
            # A block stands between these two and there is no thread yet.
            # The SAME 403 and the same key a refused send answers with —
            # never a second one. A client that could tell "refused to open"
            # from "refused to send" could tell a block from a coincidence,
            # and a key that named the block would announce it to the person
            # it is against. Note this cannot be reached when the thread
            # already exists: that is a read of history, and a block does not
            # take history away.
            return StapelErrorResponse(403, ERR_403_SEND_REFUSED)
        except services.BlockCheckUnavailable:
            # 503, never 403 and never a created thread. An outage is not
            # consent: opening the thread anyway would put a blocked party in
            # front of somebody who blocked them because a service blinked.
            return StapelErrorResponse(503, ERR_503_BLOCKS_UNAVAILABLE)

        conv = (
            Conversation.objects.prefetch_related("participants")
            .filter(id=conv.id)
            .first()
        )
        cards = services.subject_cards_for([conv])
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(
                conversation_to_dto(
                    conv,
                    _my_participant(conv, request.user),
                    cards.get(str(conv.id)),
                    services.presence_for([conv], viewer=request.user),
                )
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Chat"])
class ConversationDetailView(SerializerSeamMixin, APIView):
    """Retrieve a single conversation, or LEAVE it (participant-only).

    ``DELETE`` is the caller leaving — never a hard delete of the thread. It
    answered ``405`` until 0.8.5, so a person had no way out of a
    conversation at all and a test fixture had no way to clean one up. What
    it does and does not touch is stated once, in
    :func:`stapel_chat.services.leave_conversation`; the short version is
    that it hides the thread from the caller and takes nothing away from
    anybody else. Since 0.8.6 it is undoable: the hidden thread is listed by
    ``GET /conversations?left=true`` and put back by
    :class:`RejoinConversationView`. Staff erasure is not on this surface:
    user data has one
    deletion path in this fleet (``user.deleted`` →
    :class:`~stapel_chat.gdpr.ChatGDPRProvider`), and a second door onto the
    same rows is a second door to get wrong.
    """

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
        cards = services.subject_cards_for([conv])
        response_cls = self.get_response_serializer_class()
        return StapelResponse(
            response_cls(
                conversation_to_dto(
                    conv,
                    participant,
                    cards.get(str(conv.id)),
                    services.presence_for([conv], viewer=request.user),
                )
            )
        )

    @extend_schema(request=None, responses={204: None})
    def delete(self, request, conversation_id):  # noqa: R007
        """Leave the conversation. ``204``, and ``204`` again on a retry.

        Idempotent on purpose: a client that lost the response and retried,
        and a client leaving a thread it already left, are the same request
        and get the same answer. A second call posts no second system line —
        the service returns False and writes nothing.

        A caller who is not a party gets ``403`` with the module's one
        membership key, the same answer ``GET`` on this exact URL gives them.
        """
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        if _my_participant(conv, request.user) is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        services.leave_conversation(conversation=conv, user=request.user)
        return StapelResponse(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["Chat"])
class RejoinConversationView(SerializerSeamMixin, APIView):
    """Take back a departure — ``POST /conversations/{id}/rejoin`` -> ``204``.

    The way back from ``DELETE`` on the same thread, and the reason
    ``?left=true`` exists at all: leaving destroys nothing, so a person who
    pressed it by mistake needs a listing that shows the thread and a control
    that undoes it, or the thread is reachable only by a URL they kept.

    A verb beside ``read`` rather than a ``PATCH`` on the conversation: this
    module spells its state transitions as named POSTs (``read``,
    ``activity``, ``assign``, ``resolve``, ``reopen``), and a ``PATCH`` with a
    ``left_at: null`` body would invite a caller to send some other instant —
    a field whose only legal value is the one the server writes is not a field.

    ``204``, and ``204`` again on a retry: a client that lost the response and
    a client rejoining a thread it is already in are the same request. The
    thread comes back with the badge it had and where the departure left it —
    :func:`services.rejoin_conversation` touches the read markers and the
    conversation's ``updated_at`` not at all. A caller who is not a party gets
    ``403`` with the module's one membership key: this is an undo, never a way
    into a conversation nobody put you in.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={204: None})
    def post(self, request, conversation_id):  # noqa: R007
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        if _my_participant(conv, request.user) is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        services.rejoin_conversation(conversation=conv, user=request.user)
        return StapelResponse(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["Chat"])
class ClearConversationView(SerializerSeamMixin, APIView):
    """Clear your own history of a thread — ``POST /conversations/{id}/clear``
    -> ``204``.

    The standard messenger affordance, and standard in what it does NOT do.
    The caller's participant row is stamped ``cleared_at`` and **no message is
    deleted, edited or touched in any way**: everything created at or before
    that instant simply stops being served to this caller — it leaves their
    message list, their ``unread_count``, their ``last_message`` preview and
    their ``?search=``. The other participant's thread does not change by a
    field. This is why it exists as a mark rather than a delete: a thread is
    the record of a deal between two people, a participant may erase only the
    words they wrote themselves (``error.403.chat_not_author``), and nobody at
    all may erase the system lines — a "clear history" that removed rows would
    hand either party exactly the power the rest of this module refuses them.

    The thread stays on the list, live and writable. The mark is a floor on
    ``created_at``, never a state on a message, so the next line either side
    writes is after it and is listed, counted and previewed normally — which
    is the difference between this and leaving (``DELETE`` on the same URL,
    which takes the thread off the list and leaves the history alone: the
    exact opposite half).

    **Not idempotent, on purpose.** Clearing again moves the mark to now, and
    a client that lost the response and retried has cleared a thread it had
    just cleared — which changes nothing it can see unless something arrived
    in between, in which case moving the mark is what the person asked for.
    ``204`` every time, because there is nothing to say: the new mark is on
    the conversation (``cleared_at``) and on the caller's own inbox stream
    (``chat.conversation.cleared``), which is where a second tab learns of it.

    A caller who is not a party gets ``403`` with the module's one membership
    key, the same answer ``GET`` on this conversation gives them.
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={204: None})
    def post(self, request, conversation_id):  # noqa: R007
        conv = _get_conversation(request, conversation_id)
        if conv is None:
            return StapelErrorResponse(404, ERR_404_CONVERSATION_NOT_FOUND)
        if _my_participant(conv, request.user) is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        services.clear_conversation(conversation=conv, user=request.user)
        return StapelResponse(status=status.HTTP_204_NO_CONTENT)


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
        participant = _my_participant(conv, request.user)
        if participant is None:
            return StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        # History starts where this reader's own `cleared_at` says it does —
        # the one rule (`services.visible_messages`) the single-message read
        # and the socket's replay go through too, so a message that is off
        # this person's thread cannot come back through another door.
        qs = services.visible_messages(
            Message.objects.filter(conversation=conv),
            cleared_at=participant.cleared_at,
        ).select_related("sender")
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
        except services.SendRefused:
            # 403 with a key that names no block. The blocked party is told
            # the message did not send, and nothing else — not who blocked
            # whom, not that a block exists. Anything more turns a quiet
            # boundary into a notification.
            return StapelErrorResponse(403, ERR_403_SEND_REFUSED)
        except services.BlockCheckUnavailable:
            # 503, never 403 and never a delivered message. An outage is not
            # consent, and a 403 here would tell a sender they are blocked
            # when in fact the block store is down.
            return StapelErrorResponse(503, ERR_503_BLOCKS_UNAVAILABLE)
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
        participant = _my_participant(conv, request.user)
        if participant is None:
            return None, None, StapelErrorResponse(403, ERR_403_NOT_PARTICIPANT)
        # A message this caller cleared is not in their thread, so it is not
        # found by its id either: 404, the same answer the history gives by
        # simply not listing it. Anything else would leave the one door open
        # through which a cleared message can still be read back.
        msg = services.visible_messages(
            Message.objects.filter(pk=message_id, conversation=conv),
            cleared_at=participant.cleared_at,
        ).first()
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
        # ONE card call per subject type for the whole page. Resolving per
        # conversation would make a fifty-row inbox fifty round trips, which
        # is why the provider contract is a batch in the first place.
        cards = services.subject_cards_for(page)
        # Same batching rule for presence: one query for every participant on
        # the page, never one per row.
        presence = services.presence_for(page, viewer=request.user)
        response_cls = self.get_response_serializer_class()
        items = [
            response_cls(
                conversation_to_dto(
                    c, _my_participant(c, request.user), cards.get(str(c.id)), presence
                )
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
            response_cls(
                conversation_to_dto(
                    conv,
                    _my_participant(conv, request.user),
                    presence=services.presence_for([conv], viewer=request.user),
                )
            )
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
            response_cls(
                conversation_to_dto(
                    conv,
                    _my_participant(conv, request.user),
                    presence=services.presence_for([conv], viewer=request.user),
                )
            )
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
