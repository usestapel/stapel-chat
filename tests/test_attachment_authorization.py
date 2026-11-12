"""Who gets a resolved attachment URL — decided here, before the CDN is asked.

stapel-cdn's describe signs a link for whoever calls it with a ref, so "may
this reader see this attachment" is this module's decision. These tests hold
it at the choke point: a describe call. A stranger who holds a conversation
id, a message id and an attachment index reaches no describe through ANY
mounted route, and reads no signed URL in any answer.

``test_no_route_resolves_for_a_stranger`` walks every URL pattern the module
mounts, with every method, so a new door is covered the day it is added.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from stapel_chat import services
from stapel_chat.urls_v1 import urlpatterns

from .test_attachment_urls import VOICE, _send

pytestmark = pytest.mark.django_db

PREFIX = "/chat/api/v1/"


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def stranger(db):
    return get_user_model().objects.create_user(
        username="mallory", email="mallory@example.com", password="x"
    )


@pytest.fixture
def thread(cdn, user, other_user):
    conv = services.create_direct(owner=user, other_user_id=other_user.id)
    msg = _send(conv, user, VOICE)
    cdn.calls.clear()
    return conv, msg


def _concrete(pattern, conv, msg) -> str:
    route = str(pattern.pattern)
    for token, value in (
        ("<uuid:conversation_id>", str(conv.id)),
        ("<uuid:message_id>", str(msg.id)),
        ("<int:index>", "0"),
    ):
        route = route.replace(token, value)
    assert "<" not in route, f"unmapped converter in {route}; extend _concrete"
    return PREFIX + route


def _routes(conv, msg):
    return [_concrete(p, conv, msg) for p in urlpatterns]


def test_the_walk_sees_every_door(thread):
    conv, msg = thread
    routes = _routes(conv, msg)
    assert len(routes) == len(urlpatterns) >= 13
    assert any(r.endswith("/attachments/0") for r in routes)
    assert any(r.endswith(f"/messages/{msg.id}") for r in routes)


def test_no_route_resolves_for_a_stranger(cdn, thread, stranger):
    conv, msg = thread
    client = _client(stranger)
    for route in _routes(conv, msg):
        for method in ("get", "post", "patch", "put", "delete"):
            response = getattr(client, method)(route, {}, format="json")
            assert "/signed/" not in response.content.decode(), (method, route)
            assert response.status_code != 302, (method, route)
    assert cdn.calls == [], "a stranger reached a describe"


def test_the_same_walk_does_resolve_for_a_participant(cdn, thread, other_user):
    """Inversion: the walk above is able to see a resolution happen."""
    conv, msg = thread
    client = _client(other_user)
    seen = []
    for route in _routes(conv, msg):
        response = client.get(route)
        if response.status_code == 302 or "/signed/" in response.content.decode():
            seen.append(route)
    assert cdn.calls, "a participant's read describes"
    assert any(r.endswith("/messages") for r in seen)
    assert any(r.endswith("/attachments/0") for r in seen)


def test_a_stranger_gets_nothing_from_the_link_or_the_history(cdn, thread, stranger):
    conv, msg = thread
    client = _client(stranger)
    link = client.get(f"{PREFIX}conversations/{conv.id}/messages/{msg.id}/attachments/0")
    history = client.get(f"{PREFIX}conversations/{conv.id}/messages")
    assert link.status_code == 403 and history.status_code == 403
    assert link.json()["localizable_error"] == "error.403.chat_not_participant"
    assert cdn.calls == []


def test_a_message_from_another_thread_is_not_reachable_through_mine(
    cdn, thread, stranger, user
):
    """Holding a thread of one's own does not open another thread's message."""
    conv, msg = thread
    own = services.create_direct(owner=stranger, other_user_id=user.id)
    r = _client(stranger).get(
        f"{PREFIX}conversations/{own.id}/messages/{msg.id}/attachments/0"
    )
    assert r.status_code == 404
    assert cdn.calls == []


def test_a_removed_participant_loses_the_link(cdn, thread, other_user):
    from stapel_chat.models import ConversationParticipant

    conv, msg = thread
    ConversationParticipant.objects.filter(conversation=conv, user=other_user).delete()
    r = _client(other_user).get(
        f"{PREFIX}conversations/{conv.id}/messages/{msg.id}/attachments/0"
    )
    assert r.status_code == 403
    assert cdn.calls == []


def test_a_cleared_message_is_not_reopened_by_its_link(cdn, thread, other_user):
    conv, msg = thread
    services.clear_conversation(conversation=conv, user=other_user)
    r = _client(other_user).get(
        f"{PREFIX}conversations/{conv.id}/messages/{msg.id}/attachments/0"
    )
    assert r.status_code == 404
    assert cdn.calls == []


class TestSupport:
    def test_only_the_assigned_operator_resolves(self, cdn, user, operator_user, stranger):
        conv = services.create_support(customer=user)
        msg = _send(conv, user, VOICE)
        link = f"{PREFIX}conversations/{conv.id}/messages/{msg.id}/attachments/0"
        cdn.calls.clear()
        assert _client(operator_user).get(link).status_code == 403
        assert cdn.calls == [], "an operator who has not taken the thread is a stranger"
        services.assign_operator(conversation=conv, operator=operator_user)
        r = _client(operator_user).get(link)
        assert r.status_code == 302
        assert _client(stranger).get(link).status_code == 403


def test_the_moderation_read_hands_out_refs_not_urls(cdn, thread):
    from stapel_chat.functions import moderation_content

    _, msg = thread
    content = moderation_content({"message_id": str(msg.id)})
    assert content["media"] == [VOICE]
    assert cdn.calls == []
