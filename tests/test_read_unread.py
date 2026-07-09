"""Read markers + unread counting."""
import pytest

from stapel_chat import services
from stapel_chat.models import ConversationParticipant


@pytest.mark.django_db
class TestReadUnread:
    def _pair(self, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        return conv

    def _participant(self, conv, u):
        return ConversationParticipant.objects.get(conversation=conv, user=u)

    def test_unread_counts_only_others_messages(self, user, other_user):
        conv = self._pair(user, other_user)
        services.post_message(conversation=conv, sender=other_user, body="1")
        services.post_message(conversation=conv, sender=other_user, body="2")
        services.post_message(conversation=conv, sender=user, body="mine")
        # `user`'s unread = 2 (own message doesn't count).
        assert services.unread_count(
            conversation=conv, participant=self._participant(conv, user)
        ) == 2

    def test_system_messages_do_not_raise_unread(self, user, other_user):
        conv = self._pair(user, other_user)
        from stapel_chat.models import MessageKind

        services.post_message(
            conversation=conv, sender=None, kind=MessageKind.SYSTEM, body="sys"
        )
        assert services.unread_count(
            conversation=conv, participant=self._participant(conv, user)
        ) == 0

    def test_mark_read_advances_and_never_regresses(self, auth_client, user, other_user):
        conv = self._pair(user, other_user)
        for _ in range(3):
            services.post_message(conversation=conv, sender=other_user, body="x")
        # Mark read up to seq 2.
        r = auth_client.post(
            f"/chat/api/conversations/{conv.id}/read", {"upto_seq": 2}, format="json"
        )
        assert r.status_code == 200 and r.json()["updated"] is True
        assert services.unread_count(
            conversation=conv, participant=self._participant(conv, user)
        ) == 1
        # A lower mark is a no-op (never regresses).
        r = auth_client.post(
            f"/chat/api/conversations/{conv.id}/read", {"upto_seq": 1}, format="json"
        )
        assert r.json()["updated"] is False
        p = self._participant(conv, user)
        assert p.last_read_seq == 2

    def test_mark_read_requires_participation(self, api_client, user, other_user):
        conv = services.create_group(owner=other_user)
        api_client.force_authenticate(user=user)
        r = api_client.post(
            f"/chat/api/conversations/{conv.id}/read", {"upto_seq": 1}, format="json"
        )
        assert r.status_code == 403
