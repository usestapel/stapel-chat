"""Editing, and the tombstone.

The load-bearing claim under test: **a delete does not remove a row**. The
message id keeps being served — by history, by replay, by the delete response
itself — with its content stripped and ``deleted`` set, so that every client
cache and offline database learns which id to purge. A row that vanished would
leave those copies in place forever, which is the opposite of what a delete is
for.

The second claim, less obvious and just as load-bearing: a mutation takes a
**fresh journal sequence**. Anchored on ``seq`` an edit would sit behind a
cursor the client had already acknowledged and could never be delivered again.
"""
import pytest

from stapel_chat import services
from stapel_chat.models import Message

pytestmark = pytest.mark.django_db


def _direct(user, other):
    return services.create_direct(owner=user, other_user_id=other.id)


def _url(conv, msg=None):
    base = f"/chat/api/v1/conversations/{conv.id}/messages"
    return base if msg is None else f"{base}/{msg.id}"


class TestEdit:
    def test_edit_sets_the_flag_and_the_timestamp(self, auth_client, user, other_user):
        conv = _direct(user, other_user)
        msg = services.post_message(conversation=conv, sender=user, body="teh")
        r = auth_client.patch(_url(conv, msg), {"body": "the"}, format="json")
        assert r.status_code == 200
        body = r.json()
        assert body["body"] == "the"
        assert body["edited"] is True
        assert body["edited_at"] is not None

    def test_edit_takes_a_fresh_rev_seq_and_leaves_seq_alone(
        self, auth_client, user, other_user
    ):
        conv = _direct(user, other_user)
        first = services.post_message(conversation=conv, sender=user, body="one")
        services.post_message(conversation=conv, sender=other_user, body="two")
        r = auth_client.patch(_url(conv, first), {"body": "one!"}, format="json")
        body = r.json()
        assert body["seq"] == 1, "the thread position is immutable"
        assert body["rev_seq"] == 3, "the journal moved past both messages"

    def test_editing_does_not_reorder_history(self, auth_client, user, other_user):
        conv = _direct(user, other_user)
        first = services.post_message(conversation=conv, sender=user, body="one")
        services.post_message(conversation=conv, sender=other_user, body="two")
        auth_client.patch(_url(conv, first), {"body": "one!"}, format="json")
        r = auth_client.get(_url(conv))
        seqs = [m["seq"] for m in r.json()["items"]]
        assert seqs == sorted(seqs, reverse=True)

    def test_a_stranger_may_not_edit(self, auth_client, user, other_user):
        conv = _direct(user, other_user)
        msg = services.post_message(conversation=conv, sender=other_user, body="mine")
        r = auth_client.patch(_url(conv, msg), {"body": "yours"}, format="json")
        assert r.status_code == 403
        assert r.json()["localizable_error"] == "error.403.chat_not_author"

    def test_a_system_line_is_not_editable(self, auth_client, user, other_user):
        conv = _direct(user, other_user)
        msg = services.post_message(
            conversation=conv, sender=None, kind="system", body="chat.joined"
        )
        r = auth_client.patch(_url(conv, msg), {"body": "nope"}, format="json")
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_not_editable"

    def test_edit_window_closes(self, auth_client, user, other_user, settings):
        from datetime import timedelta

        from django.utils import timezone

        settings.STAPEL_CHAT = {"EDIT_WINDOW_S": 60}
        conv = _direct(user, other_user)
        msg = services.post_message(conversation=conv, sender=user, body="old")
        Message.objects.filter(pk=msg.pk).update(
            created_at=timezone.now() - timedelta(minutes=5)
        )
        msg.refresh_from_db()
        r = auth_client.patch(_url(conv, msg), {"body": "new"}, format="json")
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_not_editable"

    def test_an_edit_cannot_empty_a_message(self, auth_client, user, other_user):
        """Emptying is what delete is for, and delete leaves a tombstone."""
        conv = _direct(user, other_user)
        msg = services.post_message(conversation=conv, sender=user, body="text")
        r = auth_client.patch(_url(conv, msg), {"body": "   "}, format="json")
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_empty_message"


class TestTombstone:
    def test_delete_keeps_the_row_and_strips_it(self, auth_client, user, other_user):
        conv = _direct(user, other_user)
        msg = services.post_message(
            conversation=conv,
            sender=user,
            body="secret",
            attachments=[{"key": "file/x", "type": "file"}],
        )
        r = auth_client.delete(_url(conv, msg))
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == str(msg.id), "the id is the whole point"
        assert body["deleted"] is True
        assert body["deleted_at"] is not None
        assert body["body"] == ""
        assert body["attachments"] == []
        assert Message.objects.filter(pk=msg.pk).exists()

    def test_the_tombstone_is_still_served_by_history(
        self, auth_client, user, other_user
    ):
        conv = _direct(user, other_user)
        msg = services.post_message(conversation=conv, sender=user, body="bye")
        auth_client.delete(_url(conv, msg))
        r = auth_client.get(_url(conv))
        ids = [m["id"] for m in r.json()["items"]]
        assert str(msg.id) in ids, "an id that stops arriving can never be purged"
        [row] = [m for m in r.json()["items"] if m["id"] == str(msg.id)]
        assert row["deleted"] is True and row["body"] == ""

    def test_delete_does_not_renumber_the_thread(self, auth_client, user, other_user):
        conv = _direct(user, other_user)
        first = services.post_message(conversation=conv, sender=user, body="one")
        second = services.post_message(conversation=conv, sender=user, body="two")
        auth_client.delete(_url(conv, first))
        second.refresh_from_db()
        first.refresh_from_db()
        assert first.seq == 1 and second.seq == 2
        assert first.rev_seq == 3, "the tombstone is a new journal entry"

    def test_delete_is_idempotent(self, auth_client, user, other_user):
        conv = _direct(user, other_user)
        msg = services.post_message(conversation=conv, sender=user, body="bye")
        auth_client.delete(_url(conv, msg))
        msg.refresh_from_db()
        first_rev = msg.rev_seq
        r = auth_client.delete(_url(conv, msg))
        assert r.status_code == 200
        msg.refresh_from_db()
        assert msg.rev_seq == first_rev, "a retried delete consumes no sequence"

    def test_a_tombstone_cannot_be_edited_back_to_life(
        self, auth_client, user, other_user
    ):
        conv = _direct(user, other_user)
        msg = services.post_message(conversation=conv, sender=user, body="bye")
        auth_client.delete(_url(conv, msg))
        r = auth_client.patch(_url(conv, msg), {"body": "back"}, format="json")
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_message_deleted"

    def test_a_stranger_may_not_delete(self, auth_client, user, other_user):
        conv = _direct(user, other_user)
        msg = services.post_message(conversation=conv, sender=other_user, body="mine")
        r = auth_client.delete(_url(conv, msg))
        assert r.status_code == 403
        assert r.json()["localizable_error"] == "error.403.chat_not_author"

    def test_a_tombstone_raises_no_unread_badge(self, user, other_user):
        """A message deleted before you read it must not leave a badge you can
        never clear by reading anything."""
        conv = _direct(user, other_user)
        msg = services.post_message(conversation=conv, sender=other_user, body="hi")
        participant = conv.participants.get(user=user)
        assert services.unread_count(conversation=conv, participant=participant) == 1
        services.delete_message(message=msg, actor=other_user)
        assert services.unread_count(conversation=conv, participant=participant) == 0

    def test_message_of_another_conversation_is_404(
        self, auth_client, user, other_user, operator_user
    ):
        conv = _direct(user, other_user)
        elsewhere = services.create_group(owner=user, participant_ids=[operator_user.id])
        stray = services.post_message(conversation=elsewhere, sender=user, body="x")
        r = auth_client.delete(f"/chat/api/v1/conversations/{conv.id}/messages/{stray.id}")
        assert r.status_code == 404
        assert r.json()["localizable_error"] == "error.404.chat_message_not_found"


class TestErasureTombstones:
    def test_gdpr_delete_tombstones_instead_of_removing(self, user, other_user):
        """The erasure path publishes the destruction it performs.

        Hard-deleting the rows destroyed the content on the server and left
        every copy on every other participant's device, because nothing told
        those devices which ids had ceased to exist. It also tore gaps in a
        sequence the whole protocol assumes is gapless.
        """
        from stapel_chat.gdpr import ChatGDPRProvider

        conv = services.create_group(owner=user, participant_ids=[other_user.id])
        first = services.post_message(conversation=conv, sender=user, body="private")
        second = services.post_message(conversation=conv, sender=user, body="also")
        kept = services.post_message(conversation=conv, sender=other_user, body="theirs")

        ChatGDPRProvider().delete(user.id)

        first.refresh_from_db()
        second.refresh_from_db()
        kept.refresh_from_db()
        assert first.deleted_at is not None and first.body == ""
        assert first.sender_id is None
        assert second.deleted_at is not None
        assert kept.body == "theirs", "other people's messages are untouched"
        # Distinct journal positions: the socket deduplicates by seq, so a
        # shared one would swallow every tombstone but the first.
        assert first.rev_seq != second.rev_seq
        assert {first.rev_seq, second.rev_seq} == {4, 5}
