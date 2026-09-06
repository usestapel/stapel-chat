"""Leaving a conversation — ``DELETE /conversations/{id}``.

The verb hides a thread for the caller and takes nothing away from anybody
else, so every test here is paired: what the leaver stops seeing, and what the
other participant keeps. The pairing is the point — a "leave" that quietly
became a delete would pass the first half of every one of these on its own.
"""
import pytest
from rest_framework.test import APIClient

from stapel_chat import services
from stapel_chat.models import ConversationParticipant, Message, MessageKind

LIST_URL = "/chat/api/v1/conversations"


def _url(conversation_id) -> str:
    return f"{LIST_URL}/{conversation_id}"


def _client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def thread(user, other_user):
    """A direct thread with one message from the OTHER side — so the caller's
    row starts out on their list, with an unread badge on it."""
    conv = services.create_direct(owner=user, other_user_id=other_user.id)
    services.post_message(
        conversation=conv, sender=other_user, body="Is the bicycle still there?"
    )
    return conv


def _rows(client, **params):
    response = client.get(LIST_URL, params or None)
    assert response.status_code == 200, response.content
    return response.json()["items"]


# ── The leaver ───────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_leaving_takes_the_thread_off_the_list_and_out_of_the_counts(
    auth_client, thread
):
    before = _rows(auth_client)
    assert [r["id"] for r in before] == [str(thread.id)]
    assert before[0]["unread_count"] == 1

    assert auth_client.delete(_url(thread.id)).status_code == 204

    assert _rows(auth_client) == []
    # The unread chip reads the same rule the badge does, so it must agree.
    assert _rows(auth_client, unread="true") == []
    # …and so must the search, which walks the same list.
    assert _rows(auth_client, search="bicycle") == []


@pytest.mark.django_db
def test_the_departure_line_does_not_bring_the_thread_back(auth_client, user, thread):
    """Leaving posts a system line INTO the thread it just left. A rule that
    resurfaced on any new message would put the thread back in the leaver's
    inbox with their own goodbye drawn on the row."""
    auth_client.delete(_url(thread.id))
    assert _rows(auth_client) == []

    line = Message.objects.filter(conversation=thread).order_by("-seq").first()
    assert line.kind == MessageKind.SYSTEM
    assert line.sender_id is None
    assert line.body == f"{services.SYSTEM_MARKER_LEFT}:{user.id}"


@pytest.mark.django_db
def test_the_leaver_keeps_the_history_they_had(auth_client, thread):
    """Hidden, not erased: a thread reached by its own id still answers."""
    auth_client.delete(_url(thread.id))

    detail = auth_client.get(_url(thread.id))
    assert detail.status_code == 200

    history = auth_client.get(f"{_url(thread.id)}/messages")
    assert history.status_code == 200
    bodies = [m["body"] for m in history.json()["items"]]
    assert "Is the bicycle still there?" in bodies


@pytest.mark.django_db
def test_a_second_delete_is_the_same_answer_and_writes_nothing(auth_client, thread):
    assert auth_client.delete(_url(thread.id)).status_code == 204
    after_first = Message.objects.filter(conversation=thread).count()

    assert auth_client.delete(_url(thread.id)).status_code == 204
    assert Message.objects.filter(conversation=thread).count() == after_first


# ── The other side ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_other_participant_keeps_the_whole_conversation(
    auth_client, user, other_user, thread
):
    auth_client.delete(_url(thread.id))

    theirs = _client_for(other_user)
    rows = _rows(theirs)
    assert [r["id"] for r in rows] == [str(thread.id)]
    # Every message is still there, and so is the person who left.
    assert Message.objects.filter(conversation=thread, sender=other_user).count() == 1
    assert {p["user_id"] for p in rows[0]["participants"]} == {
        str(user.id), str(other_user.id)
    }
    # …drawn as LEFT rather than as removed: the durable half of the line.
    left = [p for p in rows[0]["participants"] if p["user_id"] == str(user.id)][0]
    assert left["left_at"] is not None
    stayed = [p for p in rows[0]["participants"] if p["user_id"] == str(other_user.id)]
    assert stayed[0]["left_at"] is None


@pytest.mark.django_db
def test_a_new_message_from_the_other_side_brings_the_thread_back(
    auth_client, user, other_user, thread
):
    auth_client.delete(_url(thread.id))
    assert _rows(auth_client) == []

    services.post_message(conversation=thread, sender=other_user, body="Still here?")

    rows = _rows(auth_client)
    assert [r["id"] for r in rows] == [str(thread.id)]
    assert ConversationParticipant.objects.get(
        conversation=thread, user=user
    ).left_at is None
    # The read marker was never touched, so nothing that was unread became read.
    assert rows[0]["unread_count"] == 2


# ── Refusals ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_stranger_is_refused_and_leaves_nothing_behind(operator_user, thread):
    stranger = _client_for(operator_user)
    response = stranger.delete(_url(thread.id))

    assert response.status_code == 403
    assert response.json()["localizable_error"] == "error.403.chat_not_participant"
    assert not ConversationParticipant.objects.filter(
        conversation=thread, user=operator_user
    ).exists()
    assert not Message.objects.filter(
        conversation=thread, kind=MessageKind.SYSTEM
    ).exists()


@pytest.mark.django_db
def test_an_unknown_conversation_is_not_found(auth_client):
    response = auth_client.delete(_url("11111111-1111-1111-1111-111111111111"))
    assert response.status_code == 404
    assert response.json()["localizable_error"] == "error.404.chat_conversation_not_found"


# ── Realtime and presence ────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_leavers_subscription_is_revoked(
    auth_client, user, thread, monkeypatch, django_capture_on_commit_callbacks
):
    from stapel_chat import realtime

    revoked = []
    monkeypatch.setattr(
        realtime,
        "revoke_participant",
        lambda conversation_id, user_id, reason="left_conversation": revoked.append(
            (str(conversation_id), str(user_id), reason)
        ),
    )
    with django_capture_on_commit_callbacks(execute=True):
        assert auth_client.delete(_url(thread.id)).status_code == 204

    assert (str(thread.id), str(user.id), "left_conversation") in revoked


@pytest.mark.django_db
def test_presence_stops_being_announced_into_a_left_thread(
    auth_client, user, thread, monkeypatch
):
    from stapel_chat import presence, realtime

    frames = []
    monkeypatch.setattr(
        realtime,
        "_signal",
        lambda stream, type_, payload: frames.append(payload)
        if type_ == realtime.SIGNAL_PRESENCE
        else None,
    )
    presence.on_connect(user.id)
    assert len(frames) == 1  # the thread is still theirs

    auth_client.delete(_url(thread.id))
    presence.on_disconnect(user.id)
    frames.clear()
    presence.on_connect(user.id)
    assert frames == []


# ── The service, directly ────────────────────────────────────────────────


@pytest.mark.django_db
def test_leave_conversation_is_idempotent(user, thread):
    assert services.leave_conversation(conversation=thread, user=user) is True
    assert services.leave_conversation(conversation=thread, user=user) is False


@pytest.mark.django_db
def test_a_system_line_resurfaces_nobody(user, thread):
    services.leave_conversation(conversation=thread, user=user)
    services.post_system_message(thread.id, "chat.support.resolved")
    assert ConversationParticipant.objects.get(
        conversation=thread, user=user
    ).left_at is not None


@pytest.mark.django_db
def test_the_leavers_own_message_brings_the_thread_back_for_them(user, thread):
    services.leave_conversation(conversation=thread, user=user)
    services.post_message(conversation=thread, sender=user, body="changed my mind")
    assert ConversationParticipant.objects.get(
        conversation=thread, user=user
    ).left_at is None
