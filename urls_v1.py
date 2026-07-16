"""v1 URL set for stapel-chat (api-versioning.md §2, §6).

No global prefix here — the root ``urls.py`` mounts this module under
``api/v1/`` and the host mounts that under ``chat/``:

    path("chat/", include("stapel_chat.urls"))   # -> /chat/api/v1/...
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
        "conversations",
        ConversationListCreateView.as_view(),
        name="chat-conversations",
    ),
    path(
        "conversations/<uuid:conversation_id>",
        ConversationDetailView.as_view(),
        name="chat-conversation-detail",
    ),
    path(
        "conversations/<uuid:conversation_id>/messages",
        MessageListCreateView.as_view(),
        name="chat-conversation-messages",
    ),
    path(
        "conversations/<uuid:conversation_id>/read",
        MarkReadView.as_view(),
        name="chat-conversation-read",
    ),
    path(
        "support/queue",
        SupportQueueView.as_view(),
        name="chat-support-queue",
    ),
    path(
        "support/conversations/<uuid:conversation_id>/assign",
        SupportAssignView.as_view(),
        name="chat-support-assign",
    ),
    path(
        "support/conversations/<uuid:conversation_id>/resolve",
        SupportResolveView.as_view(),
        name="chat-support-resolve",
    ),
    path(
        "support/conversations/<uuid:conversation_id>/reopen",
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
