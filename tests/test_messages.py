"""Messaging: send, seq allocation, body/attachment rules, reply, history windows."""
import pytest

from stapel_chat import services
from stapel_chat.models import Message


@pytest.mark.django_db
class TestSend:
    def _direct(self, user, other_user):
        return services.create_direct(owner=user, other_user_id=other_user.id)

    def test_send_allocates_monotonic_seq(self, auth_client, user, other_user):
        conv = self._direct(user, other_user)
        seqs = []
        for i in range(5):
            r = auth_client.post(
                f"/chat/api/conversations/{conv.id}/messages",
                {"body": f"m{i}"}, format="json",
            )
            assert r.status_code == 201
            seqs.append(r.json()["seq"])
        assert seqs == [1, 2, 3, 4, 5]
        conv.refresh_from_db()
        assert conv.last_seq == 5

    def test_empty_message_rejected(self, auth_client, user, other_user):
        conv = self._direct(user, other_user)
        r = auth_client.post(
            f"/chat/api/conversations/{conv.id}/messages", {"body": "   "},
            format="json",
        )
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_empty_message"

    def test_body_too_long_rejected(self, auth_client, user, other_user, settings):
        settings.STAPEL_CHAT = {"MAX_BODY_LENGTH": 5}
        conv = self._direct(user, other_user)
        r = auth_client.post(
            f"/chat/api/conversations/{conv.id}/messages", {"body": "way too long"},
            format="json",
        )
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_body_too_long"

    def test_attachment_only_message_ok(self, auth_client, user, other_user):
        conv = self._direct(user, other_user)
        r = auth_client.post(
            f"/chat/api/conversations/{conv.id}/messages",
            {"body": "", "attachments": ["chat/abc123"]}, format="json",
        )
        assert r.status_code == 201
        assert r.json()["attachments"] == ["chat/abc123"]

    def test_attachments_disabled_rejected(self, auth_client, user, other_user, settings):
        settings.STAPEL_CHAT = {"ATTACHMENTS": False}
        conv = self._direct(user, other_user)
        r = auth_client.post(
            f"/chat/api/conversations/{conv.id}/messages",
            {"body": "see file", "attachments": ["chat/x"]}, format="json",
        )
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_attachments_disabled"

    def test_reply_must_be_in_conversation(self, auth_client, user, other_user):
        conv = self._direct(user, other_user)
        other = services.create_group(owner=user)
        stray = services.post_message(conversation=other, sender=user, body="elsewhere")
        r = auth_client.post(
            f"/chat/api/conversations/{conv.id}/messages",
            {"body": "re", "reply_to": str(stray.id)}, format="json",
        )
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_invalid_reply"

    def test_reply_ok(self, auth_client, user, other_user):
        conv = self._direct(user, other_user)
        first = services.post_message(conversation=conv, sender=other_user, body="q")
        r = auth_client.post(
            f"/chat/api/conversations/{conv.id}/messages",
            {"body": "a", "reply_to": str(first.id)}, format="json",
        )
        assert r.status_code == 201
        assert r.json()["reply_to"] == str(first.id)

    def test_non_participant_cannot_send(self, api_client, user, other_user):
        conv = services.create_group(owner=other_user)
        api_client.force_authenticate(user=user)
        r = api_client.post(
            f"/chat/api/conversations/{conv.id}/messages", {"body": "hi"},
            format="json",
        )
        assert r.status_code == 403


@pytest.mark.django_db
class TestHistoryAnchorWindows:
    def _seeded(self, user, other_user, n=6):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        for i in range(1, n + 1):
            services.post_message(conversation=conv, sender=user, body=f"m{i}")
        return conv

    def test_next_walks_older_messages_closest_first(self, auth_client, user, other_user):
        conv = self._seeded(user, other_user)
        # newest-first: direction=next from anchor 5 = the older messages just
        # below it, closest first.
        r = auth_client.get(
            f"/chat/api/conversations/{conv.id}/messages?anchor=5&direction=next&limit=2"
        )
        body = r.json()
        assert [m["seq"] for m in body["items"]] == [4, 3]
        assert body["has_next"] is True

    def test_prev_returns_newer_side(self, auth_client, user, other_user):
        conv = self._seeded(user, other_user)
        r = auth_client.get(
            f"/chat/api/conversations/{conv.id}/messages?anchor=2&direction=prev&limit=2"
        )
        seqs = [m["seq"] for m in r.json()["items"]]
        assert len(seqs) == 2 and all(s > 2 for s in seqs)

    def test_anchor_is_stable_under_insertion(self, auth_client, user, other_user):
        conv = self._seeded(user, other_user)
        # First page: the two messages just older than seq 6.
        page1 = auth_client.get(
            f"/chat/api/conversations/{conv.id}/messages?anchor=6&direction=next&limit=2"
        ).json()
        assert [m["seq"] for m in page1["items"]] == [5, 4]
        # Newer messages arrive (seq 7, 8); the seq anchor is unaffected — paging
        # from the page's next_anchor still yields the next contiguous older run.
        services.post_message(conversation=conv, sender=other_user, body="m7")
        services.post_message(conversation=conv, sender=other_user, body="m8")
        page2 = auth_client.get(
            f"/chat/api/conversations/{conv.id}/messages"
            f"?anchor={page1['next_anchor']}&direction=next&limit=2"
        ).json()
        assert [m["seq"] for m in page2["items"]] == [3, 2]

    def test_full_history_is_newest_first(self, auth_client, user, other_user):
        conv = self._seeded(user, other_user)
        r = auth_client.get(
            f"/chat/api/conversations/{conv.id}/messages?limit=100"
        )
        assert [m["seq"] for m in r.json()["items"]] == [6, 5, 4, 3, 2, 1]
        assert Message.objects.filter(conversation=conv).count() == 6
