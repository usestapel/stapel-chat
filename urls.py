"""URL patterns — no global prefix here, the host project mounts them:

    path("chat/", include("stapel_chat.urls"))
"""
from typing import NamedTuple

from django.urls import path

from .views import (
    ConversationDetailView,
    ConversationListCreateView,
    MarkReadView,
    MessageListCreateView,
    SupportAssignView,
    SupportQueueView,
    SupportReopenView,
    SupportResolveView,
)

urlpatterns = [
    path(
        "api/conversations",
        ConversationListCreateView.as_view(),
        name="chat-conversations",
    ),
    path(
        "api/conversations/<uuid:conversation_id>",
        ConversationDetailView.as_view(),
        name="chat-conversation-detail",
    ),
    path(
        "api/conversations/<uuid:conversation_id>/messages",
        MessageListCreateView.as_view(),
        name="chat-conversation-messages",
    ),
    path(
        "api/conversations/<uuid:conversation_id>/read",
        MarkReadView.as_view(),
        name="chat-conversation-read",
    ),
    path(
        "api/support/queue",
        SupportQueueView.as_view(),
        name="chat-support-queue",
    ),
    path(
        "api/support/conversations/<uuid:conversation_id>/assign",
        SupportAssignView.as_view(),
        name="chat-support-assign",
    ),
    path(
        "api/support/conversations/<uuid:conversation_id>/resolve",
        SupportResolveView.as_view(),
        name="chat-support-resolve",
    ),
    path(
        "api/support/conversations/<uuid:conversation_id>/reopen",
        SupportReopenView.as_view(),
        name="chat-support-reopen",
    ),
]


class GateEntry(NamedTuple):
    """One gated URL block: which flags gate which url patterns (capability-config.md §2 p.2).

    ``flags`` compose with OR — the block is mounted while ANY flag is on, and
    disappears only when ALL of them are off. Empty flags = always on.
    """
    name: str
    flags: tuple
    patterns: tuple


#: Gate registry (capability-config.md §2 p.2): stapel-chat's axes are
#: behavioral, not URL gates — ``CHAT_KINDS`` narrows what a request may create/
#: operate (enforced in the views: support endpoints answer 400 when support is
#: not enabled), ``ATTACHMENTS`` narrows what a message may carry, and
#: ``MAX_BODY_LENGTH`` bounds a body; none unmounts an endpoint. So the whole
#: URL surface is a single always-on block, declared here so the
#: capabilities.json emitter has a uniform mechanism across every module.
GATE_REGISTRY: dict = {
    'chat.api': GateEntry('chat.api', (), tuple(urlpatterns)),
}
