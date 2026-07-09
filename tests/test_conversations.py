"""Conversation creation + listing: direct idempotency, group/support, scoping."""
import pytest

from stapel_chat import services
from stapel_chat.models import Conversation, ConversationKind


@pytest.mark.django_db
class TestCreate:
    def test_direct_is_idempotent_over_http(self, auth_client, other_user):
        payload = {"kind": "direct", "participant_ids": [str(other_user.id)]}
        r1 = auth_client.post("/chat/api/conversations", payload, format="json")
        r2 = auth_client.post("/chat/api/conversations", payload, format="json")
        assert r1.status_code == 201 and r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"]
        assert Conversation.objects.filter(kind=ConversationKind.DIRECT).count() == 1

    def test_direct_idempotent_regardless_of_initiator(self, user, other_user):
        a = services.create_direct(owner=user, other_user_id=other_user.id)
        b = services.create_direct(owner=other_user, other_user_id=user.id)
        assert a.id == b.id

    def test_direct_requires_exactly_one_other(self, auth_client, user, other_user):
        # zero others
        r = auth_client.post(
            "/chat/api/conversations", {"kind": "direct", "participant_ids": []},
            format="json",
        )
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_invalid_direct"
        # self only -> filtered out -> still zero
        r = auth_client.post(
            "/chat/api/conversations",
            {"kind": "direct", "participant_ids": [str(user.id)]}, format="json",
        )
        assert r.status_code == 400

    def test_group_creation_adds_owner_and_members(self, auth_client, user, other_user):
        r = auth_client.post(
            "/chat/api/conversations",
            {"kind": "group", "participant_ids": [str(other_user.id)]}, format="json",
        )
        assert r.status_code == 201
        ids = {p["user_id"] for p in r.json()["participants"]}
        assert ids == {str(user.id), str(other_user.id)}

    def test_support_creation_opens_thread(self, auth_client, user):
        r = auth_client.post(
            "/chat/api/conversations", {"kind": "support"}, format="json"
        )
        assert r.status_code == 201
        body = r.json()
        assert body["kind"] == "support"
        assert body["support_status"] == "open"
        assert body["assigned_operator_id"] is None

    def test_unknown_kind_rejected(self, auth_client):
        r = auth_client.post(
            "/chat/api/conversations", {"kind": "telepathy"}, format="json"
        )
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_invalid_kind"

    def test_disabled_kind_rejected(self, auth_client, settings):
        settings.STAPEL_CHAT = {"CHAT_KINDS": ["direct", "group"]}
        r = auth_client.post(
            "/chat/api/conversations", {"kind": "support"}, format="json"
        )
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_kind_disabled"


@pytest.mark.django_db
class TestListAndDetail:
    def test_list_returns_my_conversations_with_unread(
        self, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        # other_user posts two messages -> unread for `user` is 2.
        services.post_message(conversation=conv, sender=other_user, body="hi")
        services.post_message(conversation=conv, sender=other_user, body="there")
        r = auth_client.get("/chat/api/conversations")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == str(conv.id)
        assert items[0]["unread_count"] == 2

    def test_detail_requires_participation(self, api_client, user, other_user):
        # A conversation `user` is not part of.
        conv = services.create_group(owner=other_user)
        api_client.force_authenticate(user=user)
        r = api_client.get(f"/chat/api/conversations/{conv.id}")
        assert r.status_code == 403
        assert r.json()["localizable_error"] == "error.403.chat_not_participant"

    def test_detail_missing_is_404(self, auth_client):
        import uuid

        r = auth_client.get(f"/chat/api/conversations/{uuid.uuid4()}")
        assert r.status_code == 404
