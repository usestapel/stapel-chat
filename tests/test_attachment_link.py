"""The link that does not expire: ``…/messages/<id>/attachments/<index>``.

Anything kept past a page view (a download button, an e-mail, a push
notification, a bus event) carries this route rather than a signed URL that
is dead fifteen minutes later. Each request re-checks access and redirects to
a URL resolved for it.
"""
from __future__ import annotations

import pytest

from stapel_chat import services
from stapel_chat.models import Message

from .test_attachment_urls import DOC, VOICE, _send

pytestmark = pytest.mark.django_db


def _link(conv, msg, index=0):
    return f"/chat/api/v1/conversations/{conv.id}/messages/{msg.id}/attachments/{index}"


class TestRedirect:
    def test_it_redirects_to_a_url_resolved_for_this_request(
        self, cdn, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = _send(conv, other_user, VOICE)
        first = auth_client.get(_link(conv, msg))
        second = auth_client.get(_link(conv, msg))
        assert first.status_code == 302
        assert first["Location"].startswith("/cdn/api/v1/media/signed/audio/")
        assert first["Location"] != second["Location"]
        assert first["Cache-Control"] == "private, no-store"

    def test_every_attachment_on_the_wire_names_its_link(
        self, cdn, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = _send(conv, other_user, VOICE, DOC)
        r = auth_client.get(f"/chat/api/v1/conversations/{conv.id}/messages")
        hrefs = [a["href"] for a in r.json()["items"][0]["attachments"]]
        assert hrefs == [_link(conv, msg, 0), _link(conv, msg, 1)]
        assert auth_client.get(hrefs[1])["Location"].startswith(
            "/cdn/api/v1/media/signed/file/"
        )

    def test_an_unanswered_cdn_is_503_and_a_gone_object_404(
        self, cdn, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = _send(conv, other_user, VOICE, DOC)
        cdn.missing = {DOC}
        r = auth_client.get(_link(conv, msg, 1))
        assert r.status_code == 404
        assert r.json()["localizable_error"] == "error.404.chat_attachment_not_found"
        cdn.fail = True
        r = auth_client.get(_link(conv, msg, 0))
        assert r.status_code == 503
        assert r.json()["localizable_error"] == "error.503.chat_attachment_unavailable"

    def test_no_such_index_and_a_tombstone_are_404(
        self, cdn, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = _send(conv, user, VOICE)
        assert auth_client.get(_link(conv, msg, 1)).status_code == 404
        services.delete_message(message=msg, actor=user)
        cdn.calls.clear()
        assert auth_client.get(_link(conv, msg, 0)).status_code == 404
        assert cdn.calls == []


class TestBusCarriesNoSignedUrl:
    def test_the_message_action_holds_refs_and_links_only(
        self, cdn, user, other_user, captured_events
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = _send(conv, user, VOICE)
        [event] = [e for e in captured_events if e.event_type == "chat.message"]
        [attachment] = event.payload["attachments"]
        assert attachment["key"] == VOICE
        assert attachment["href"] == _link(conv, msg)
        assert "variants" not in attachment and "poster_url" not in attachment
        assert "/signed/" not in repr(event.payload)

    def test_an_edit_action_too(self, cdn, user, other_user):
        from stapel_core.comm import action_registry, subscribe_action

        seen = []

        def handler(event):
            seen.append(event)

        subscribe_action("chat.message.edited", handler)
        try:
            conv = services.create_direct(owner=user, other_user_id=other_user.id)
            msg = _send(conv, user, VOICE)
            services.edit_message(
                message=Message.objects.get(pk=msg.pk), editor=user, body="edited"
            )
        finally:
            action_registry._subscribers.get("chat.message.edited", []).remove(handler)
        assert seen and "/signed/" not in repr(seen[-1].payload)
