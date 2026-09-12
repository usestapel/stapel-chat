"""Clearing your own history — ``POST /conversations/{id}/clear``.

The verb hides a thread's past FOR ONE READER and takes nothing away from
anybody — not from the other participant, and not from the database. So every
test here is paired, the way the leave tests are: what the clearer stops being
served, and what the other side and the rows keep. The pairing is the point —
a "clear" that quietly became a delete would pass the first half of every one
of these on its own.
"""
import pytest
from rest_framework.test import APIClient

from stapel_chat import services
from stapel_chat.models import ConversationParticipant, Message, MessageKind

LIST_URL = "/chat/api/v1/conversations"


def _url(conversation_id) -> str:
    return f"{LIST_URL}/{conversation_id}"


def _clear_url(conversation_id) -> str:
    return f"{_url(conversation_id)}/clear"


def _client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def thread(user, other_user):
    """A direct thread carrying exactly what the darom demo thread carries: a
    system line nobody may delete, and an authored message from the OTHER side
    — so the caller's row starts out with an unread badge and a preview."""
    conv = services.create_direct(owner=user, other_user_id=other_user.id)
    services.post_system_message(conv.id, "video.call.ended:0")
    services.post_message(
        conversation=conv, sender=other_user, body="Is the fridge still there?"
    )
    return conv


def _rows(client, **params):
    response = client.get(LIST_URL, params or None)
    assert response.status_code == 200, response.content
    return response.json()["items"]


def _bodies(client, conversation_id):
    response = client.get(f"{_url(conversation_id)}/messages")
    assert response.status_code == 200, response.content
    return [m["body"] for m in response.json()["items"]]


# ── The clearer ──────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_list_excludes_everything_before_the_mark_for_the_clearer(
    auth_client, thread
):
    assert _bodies(auth_client, thread.id) == [
        "Is the fridge still there?",
        "video.call.ended:0",
    ]

    assert auth_client.post(_clear_url(thread.id)).status_code == 204

    # Including the system line, which no delete on this module can remove.
    assert _bodies(auth_client, thread.id) == []


@pytest.mark.django_db
def test_the_unread_count_is_recounted_over_what_is_left(auth_client, thread):
    before = _rows(auth_client)
    assert before[0]["unread_count"] == 1

    auth_client.post(_clear_url(thread.id))

    after = _rows(auth_client)
    # The thread is STILL on the list — clearing is not leaving — with nothing
    # left to count and nothing left to draw.
    assert [r["id"] for r in after] == [str(thread.id)]
    assert after[0]["unread_count"] == 0
    assert after[0]["last_message"] is None
    assert after[0]["cleared_at"] is not None
    # The unread chip reads the same rule the badge does, so it must agree.
    assert _rows(auth_client, unread="true") == []


@pytest.mark.django_db
def test_the_preview_and_the_search_lose_the_cleared_line_together(
    auth_client, thread
):
    """The row's preview and ``?search=`` read the same annotations, so a
    cleared line cannot stay findable by words the row no longer draws."""
    assert _rows(auth_client)[0]["last_message"]["body_preview"] == (
        "Is the fridge still there?"
    )
    assert [r["id"] for r in _rows(auth_client, search="fridge")] == [str(thread.id)]

    auth_client.post(_clear_url(thread.id))

    assert _rows(auth_client)[0]["last_message"] is None
    assert _rows(auth_client, search="fridge") == []


@pytest.mark.django_db
def test_a_detail_read_agrees_with_the_list(auth_client, thread):
    """One rule, not two: the single-conversation read is annotated for the
    same viewer, so the thread does not come back by its own URL."""
    auth_client.post(_clear_url(thread.id))

    detail = auth_client.get(_url(thread.id))
    assert detail.status_code == 200
    body = detail.json()
    assert body["last_message"] is None
    assert body["unread_count"] == 0
    assert body["cleared_at"] is not None


@pytest.mark.django_db
def test_a_cleared_message_is_not_readable_by_its_own_id(auth_client, user, thread):
    """The last door: a message off this person's list is 404 by id too."""
    mine = services.post_message(conversation=thread, sender=user, body="Mine")
    auth_client.post(_clear_url(thread.id))

    response = auth_client.delete(f"{_url(thread.id)}/messages/{mine.id}")
    assert response.status_code == 404
    assert response.json()["localizable_error"] == "error.404.chat_message_not_found"


@pytest.mark.django_db
def test_new_messages_show_normally_after_the_mark(auth_client, other_user, thread):
    auth_client.post(_clear_url(thread.id))

    services.post_message(
        conversation=thread, sender=other_user, body="Still available, yes."
    )

    assert _bodies(auth_client, thread.id) == ["Still available, yes."]
    rows = _rows(auth_client)
    assert rows[0]["unread_count"] == 1
    assert rows[0]["last_message"]["body_preview"] == "Still available, yes."
    assert [r["id"] for r in _rows(auth_client, search="available")] == [
        str(thread.id)
    ]


@pytest.mark.django_db
def test_clearing_again_moves_the_mark(auth_client, other_user, thread):
    """Not idempotent, on purpose: "clear history" means "from here", and a
    person looking at a thread they have written in since means a later here."""
    auth_client.post(_clear_url(thread.id))
    first = auth_client.get(_url(thread.id)).json()["cleared_at"]

    services.post_message(conversation=thread, sender=other_user, body="And now?")
    assert _bodies(auth_client, thread.id) == ["And now?"]

    assert auth_client.post(_clear_url(thread.id)).status_code == 204
    second = auth_client.get(_url(thread.id)).json()["cleared_at"]

    assert second > first
    assert _bodies(auth_client, thread.id) == []


# ── The other side, and the rows ─────────────────────────────────────────


@pytest.mark.django_db
def test_the_other_participant_is_unaffected(auth_client, user, other_user, thread):
    auth_client.post(_clear_url(thread.id))

    theirs = _client_for(other_user)
    assert _bodies(theirs, thread.id) == [
        "Is the fridge still there?",
        "video.call.ended:0",
    ]
    rows = _rows(theirs)
    assert rows[0]["last_message"]["body_preview"] == "Is the fridge still there?"
    # Their own mark is untouched, and the clearer's is not on their copy of
    # the participant list — clearing is private to the person who did it.
    assert rows[0]["cleared_at"] is None
    assert ConversationParticipant.objects.get(
        conversation=thread, user=other_user
    ).cleared_at is None
    assert all("cleared_at" not in p for p in rows[0]["participants"])


@pytest.mark.django_db
def test_not_one_message_row_is_touched(auth_client, thread):
    before = list(
        Message.objects.filter(conversation=thread)
        .order_by("seq")
        .values_list("id", "body", "deleted_at", "rev_seq")
    )

    auth_client.post(_clear_url(thread.id))

    after = list(
        Message.objects.filter(conversation=thread)
        .order_by("seq")
        .values_list("id", "body", "deleted_at", "rev_seq")
    )
    assert after == before
    assert Message.objects.filter(
        conversation=thread, kind=MessageKind.SYSTEM
    ).count() == 1


@pytest.mark.django_db
def test_clearing_posts_no_system_line(auth_client, thread):
    """Nothing the counterpart can observe, so nothing in the transcript."""
    before = Message.objects.filter(conversation=thread).count()
    auth_client.post(_clear_url(thread.id))
    assert Message.objects.filter(conversation=thread).count() == before


# ── The signal ───────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_clearer_own_clients_are_told(
    user, other_user, thread, monkeypatch, django_capture_on_commit_callbacks
):
    """``chat.conversation.cleared``, on the clearer's OWN inbox stream and
    nobody else's: their other tabs are the whole audience."""
    from stapel_chat import realtime

    sent = []
    monkeypatch.setattr(
        realtime,
        "_signal",
        lambda stream, type_, payload: sent.append((stream, type_, payload)),
    )

    with django_capture_on_commit_callbacks(execute=True):
        mark = services.clear_conversation(conversation=thread, user=user)

    frames = [f for f in sent if f[1] == realtime.SIGNAL_CLEARED]
    assert len(frames) == 1
    stream, _, payload = frames[0]
    assert stream == realtime.user_stream(user.id)
    assert stream != realtime.user_stream(other_user.id)
    assert stream != realtime.conversation_stream(thread.id)
    assert payload["conversation_id"] == str(thread.id)
    assert payload["user_id"] == str(user.id)
    assert payload["cleared_at"] == mark.isoformat()


# ── Refusals ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_stranger_is_refused_and_clears_nothing(operator_user, thread):
    response = _client_for(operator_user).post(_clear_url(thread.id))
    assert response.status_code == 403
    assert response.json()["localizable_error"] == "error.403.chat_not_participant"
    assert not ConversationParticipant.objects.filter(
        conversation=thread, cleared_at__isnull=False
    ).exists()


@pytest.mark.django_db
def test_an_unknown_conversation_is_not_found(auth_client):
    import uuid

    response = auth_client.post(_clear_url(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["localizable_error"] == "error.404.chat_conversation_not_found"


@pytest.mark.django_db
def test_the_service_refuses_a_non_participant(operator_user, thread):
    """The service is the same door as the view: no participant row, no mark
    — it never creates membership on the way to writing one."""
    assert services.clear_conversation(conversation=thread, user=operator_user) is None
    assert not ConversationParticipant.objects.filter(
        conversation=thread, user=operator_user
    ).exists()


# ── The socket ───────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_replay_is_bounded_by_the_subscribers_own_mark(user, other_user, thread):
    """The one door a REST-only rule leaves open.

    ``rev_seq`` is re-allocated on every edit, so a message from before the
    mark that the other side corrects afterwards is a row the catch-up would
    hand to the very person who cleared it. The subscriber's mark is what the
    replay is bounded by — and only that subscriber's: the counterpart, who
    cleared nothing, still receives the correction.
    """
    from stapel_chat.consumers import _replay

    old = Message.objects.filter(
        conversation=thread, sender=other_user
    ).get()
    services.clear_conversation(conversation=thread, user=user)
    services.edit_message(
        message=old, editor=other_user, body="Is the fridge still for sale?"
    )

    assert _replay(thread.id, 0, 50, user.id) == []
    theirs = _replay(thread.id, 0, 50, other_user.id)
    assert [row.payload["body"] for row in theirs] == [
        "video.call.ended:0",
        "Is the fridge still for sale?",
    ]
