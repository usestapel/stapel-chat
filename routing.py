"""Channels routes — discovered, not hand-wired.

``stapel_realtime.build_websocket_application()`` walks INSTALLED_APPS and
collects every ``<app>.routing.websocket_urlpatterns``, so a host that assembles
its ASGI app the canonical way gets both chat sockets without naming either::

    # asgi.py — the whole file
    from django.core.asgi import get_asgi_application
    from stapel_realtime.asgi import build_websocket_application

    application = build_websocket_application(
        http_application=get_asgi_application()
    )

That replaces the hand-written ``ProtocolTypeRouter`` this module used to ask
for. The hand-written one is how the fleet ended up with three different auth
stacks and three close-code sets — and, in the deployment that prompted 0.3.0,
with an origin guard nobody had put in front of a socket that authenticates by
cookie.

Two mounts, because a chat has two live surfaces:

    ws/chat/<uuid:conversation_id>   one thread's journal (resumable)
    ws/chat/inbox                    this user's conversation list (ephemeral)

The inbox route carries **no** user segment: the consumer derives its stream
key from the authenticated scope, so there is nothing in the URL to tamper
with.
"""
from django.urls import path

from .consumers import ChatConsumer, ChatInboxConsumer

websocket_urlpatterns = [
    path("ws/chat/inbox", ChatInboxConsumer.as_asgi()),
    path("ws/chat/<uuid:conversation_id>", ChatConsumer.as_asgi()),
]
