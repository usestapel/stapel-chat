"""The way back — ``?left=true`` and ``POST /conversations/{id}/rejoin``.

0.8.5 gave a person a way out of a thread and no way back to it: the thread
was correctly off the list, off the counts and off ``?search=``, and no
listing showed it, so a mistaken "leave" was undoable only by a URL somebody
had kept. Every test here is paired the way the leave tests are — what the
left listing SHOWS, and what the default listing still refuses to.
"""
import pytest
from rest_framework.test import APIClient

from stapel_chat import services
from stapel_chat.models import Conversation, ConversationParticipant

LIST_URL = "/chat/api/v1/conversations"


def _rejoin_url(conversation_id) -> str:
    return f"{LIST_URL}/{conversation_id}/rejoin"


def _client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _user(username):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="x"
    )


def _rows(client, **params):
    response = client.get(LIST_URL, params or None)
    assert response.status_code == 200, response.content
    return response.json()["items"]


@pytest.fixture
def thread(user, other_user):
    """A direct thread with one message from the OTHER side — on the caller's
    list with an unread badge on it, so "the badge it had" is a real number."""
    conv = services.create_direct(owner=user, other_user_id=other_user.id)
    services.post_message(
        conversation=conv, sender=other_user, body="Is the bicycle still there?"
    )
    return conv


# ── The listing ──────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_left_list_shows_only_left_threads_and_the_default_one_shows_the_rest(
    auth_client, user, other_user, thread
):
    """The two listings are complements: every thread is on exactly one."""
    stayed = services.create_direct(
        owner=user, other_user_id=_user("carol").id, subject_type="", subject_key=""
    )
    services.post_message(conversation=stayed, sender=user, body="still talking")

    assert auth_client.delete(f"{LIST_URL}/{thread.id}").status_code == 204

    left = _rows(auth_client, left="true")
    assert [r["id"] for r in left] == [str(thread.id)]
    assert _rows(auth_client, left="true", limit=50) == left

    default = _rows(auth_client)
    assert [r["id"] for r in default] == [str(stayed.id)]


@pytest.mark.django_db
def test_a_thread_nobody_left_is_on_no_left_list(auth_client, thread):
    assert _rows(auth_client, left="true") == []
    assert [r["id"] for r in _rows(auth_client)] == [str(thread.id)]


@pytest.mark.django_db
def test_every_row_carries_left_at_and_the_default_list_carries_none(
    auth_client, user, thread
):
    auth_client.delete(f"{LIST_URL}/{thread.id}")

    row = _rows(auth_client, left="true")[0]
    assert row["left_at"] is not None
    # …and it is the CALLER's stamp, not somebody else's: the same instant
    # their own participant row carries.
    mine = [p for p in row["participants"] if p["user_id"] == str(user.id)][0]
    assert mine["left_at"] == row["left_at"]

    services.rejoin_conversation(conversation=thread, user=user)
    assert _rows(auth_client)[0]["left_at"] is None


@pytest.mark.django_db
def test_the_left_list_is_ordered_by_departure_newest_first(
    auth_client, user, thread
):
    others = [
        services.create_direct(owner=user, other_user_id=_user(f"dave{i}").id)
        for i in range(3)
    ]
    for conv in others:
        services.post_message(conversation=conv, sender=user, body="hello")
    # Leave them in a known order, LAST one first on the list.
    order = [others[1], others[0], others[2]]
    for conv in order:
        assert auth_client.delete(f"{LIST_URL}/{conv.id}").status_code == 204

    rows = _rows(auth_client, left="true")
    assert [r["id"] for r in rows] == [str(c.id) for c in reversed(order)]
    # Ordered by the CALLER's departure: the timestamps come back descending.
    assert [r["left_at"] for r in rows] == sorted(
        [r["left_at"] for r in rows], reverse=True
    )


@pytest.mark.django_db
def test_a_thread_both_parties_left_is_ordered_by_the_callers_own_stamp(
    auth_client, user, other_user, thread
):
    """The counterpart's departure must never decide where a row sits.

    Ordering on the joined participant column takes whichever row the join
    produced, which on a thread two people walked out of is a coin flip.
    """
    services.leave_conversation(conversation=thread, user=other_user)
    later = services.create_direct(owner=user, other_user_id=_user("erin").id)
    services.post_message(conversation=later, sender=user, body="hello")
    auth_client.delete(f"{LIST_URL}/{thread.id}")
    auth_client.delete(f"{LIST_URL}/{later.id}")

    rows = _rows(auth_client, left="true")
    assert [r["id"] for r in rows] == [str(later.id), str(thread.id)]


@pytest.mark.django_db
def test_search_and_unread_compose_on_the_left_list(auth_client, other_user, thread):
    auth_client.delete(f"{LIST_URL}/{thread.id}")

    # The same rule as the inbox, unchanged: a row is found by what it DRAWS.
    # The counterpart's name is the field that still finds a left thread —
    # its last line is now the departure marker, which draws nothing and is
    # searched by nothing (`drawn_last_line`), exactly as on the inbox.
    assert other_user.username == "bob"
    assert [r["id"] for r in _rows(auth_client, left="true", search="bob")] == [
        str(thread.id)
    ]
    assert _rows(auth_client, left="true", search="bicycle") == []
    assert _rows(auth_client, left="true", search="zebra") == []
    # The badge survived the departure, so the chip finds the row.
    assert [r["id"] for r in _rows(auth_client, left="true", unread="true")] == [
        str(thread.id)
    ]
    # …and the same two filters still find nothing on the default list.
    assert _rows(auth_client, search="bob") == []
    assert _rows(auth_client, unread="true") == []


@pytest.mark.django_db
def test_the_left_list_pages_on_the_departure_anchor(auth_client, user):
    convs = []
    for i in range(4):
        conv = services.create_direct(owner=user, other_user_id=_user(f"fay{i}").id)
        services.post_message(conversation=conv, sender=user, body="hello")
        convs.append(conv)
    for conv in convs:
        auth_client.delete(f"{LIST_URL}/{conv.id}")

    first = auth_client.get(LIST_URL, {"left": "true", "limit": 2}).json()
    assert first["count"] == 2 and first["has_next"]
    assert first["next_anchor"] is not None
    second = auth_client.get(
        LIST_URL, {"left": "true", "limit": 2, "anchor": first["next_anchor"]}
    ).json()
    seen = [r["id"] for r in first["items"]] + [r["id"] for r in second["items"]]
    assert seen == [str(c.id) for c in reversed(convs)]


@pytest.mark.django_db
def test_a_stranger_sees_nobody_elses_departures(operator_user, user, thread):
    services.leave_conversation(conversation=thread, user=user)
    assert _rows(_client_for(operator_user), left="true") == []


@pytest.mark.django_db
def test_any_other_value_of_left_is_the_default_list(auth_client, thread):
    for value in ("false", "0", "", "yes-please"):
        assert [r["id"] for r in _rows(auth_client, left=value)] == [str(thread.id)]


# ── Rejoining ────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_rejoin_returns_the_thread_with_the_badge_it_had(
    auth_client, user, thread
):
    # Read the first message, so "the badge it had" is a marker that MOVED and
    # a rejoin that reset it would be visible.
    services.post_message(
        conversation=thread, sender=thread.participants.exclude(user=user).first().user,
        body="and the helmet?",
    )
    auth_client.post(
        f"{LIST_URL}/{thread.id}/read", {"upto_seq": 1}, format="json"
    )
    before = _rows(auth_client)[0]
    assert before["unread_count"] == 1
    marker = ConversationParticipant.objects.get(
        conversation=thread, user=user
    ).last_read_seq
    assert marker == 1

    assert auth_client.delete(f"{LIST_URL}/{thread.id}").status_code == 204
    assert _rows(auth_client) == []

    assert auth_client.post(_rejoin_url(thread.id)).status_code == 204

    rows = _rows(auth_client)
    assert [r["id"] for r in rows] == [str(thread.id)]
    # The read marker was never touched, so the badge is the one it had.
    assert rows[0]["unread_count"] == before["unread_count"]
    assert rows[0]["left_at"] is None
    assert (
        ConversationParticipant.objects.get(
            conversation=thread, user=user
        ).last_read_seq
        == marker
    )
    # …and it is off the left list, which is the complement.
    assert _rows(auth_client, left="true") == []


@pytest.mark.django_db
def test_rejoin_is_idempotent_and_writes_no_line(auth_client, thread):
    from stapel_chat.models import Message

    auth_client.delete(f"{LIST_URL}/{thread.id}")
    after_leaving = Message.objects.filter(conversation=thread).count()

    assert auth_client.post(_rejoin_url(thread.id)).status_code == 204
    assert auth_client.post(_rejoin_url(thread.id)).status_code == 204
    # Coming back is silent: the state a client renders from is the cleared
    # `left_at`, and a second line per undo would be noise about a mistake.
    assert Message.objects.filter(conversation=thread).count() == after_leaving


@pytest.mark.django_db
def test_rejoining_a_thread_never_left_changes_nothing(auth_client, user, thread):
    updated_at = Conversation.objects.get(pk=thread.pk).updated_at

    assert auth_client.post(_rejoin_url(thread.id)).status_code == 204

    assert Conversation.objects.get(pk=thread.pk).updated_at == updated_at
    assert (
        ConversationParticipant.objects.get(conversation=thread, user=user).left_at
        is None
    )


@pytest.mark.django_db
def test_rejoin_does_not_move_the_thread_on_the_list(auth_client, user, thread):
    """Coming back is not activity: `updated_at` is not touched by a rejoin.

    The DEPARTURE moves a thread (it posts a system line, which allocates a
    seq and stamps the conversation) — that is the leave verb's doing and it
    is already sealed. The undo adds nothing on top: the row returns to the
    inbox exactly where the departure left it, so a person restoring five
    threads does not shuffle their inbox five times.
    """
    newer = services.create_direct(owner=user, other_user_id=_user("gil").id)
    services.post_message(conversation=newer, sender=user, body="later thread")

    auth_client.delete(f"{LIST_URL}/{thread.id}")
    settled = Conversation.objects.get(pk=thread.pk).updated_at

    auth_client.post(_rejoin_url(thread.id))

    assert Conversation.objects.get(pk=thread.pk).updated_at == settled
    assert [r["id"] for r in _rows(auth_client)] == [str(thread.id), str(newer.id)]


@pytest.mark.django_db
def test_a_stranger_is_refused_and_joins_nothing(operator_user, thread):
    response = _client_for(operator_user).post(_rejoin_url(thread.id))

    assert response.status_code == 403
    assert response.json()["localizable_error"] == "error.403.chat_not_participant"
    # An undo, never a door in: no participant row was created.
    assert not ConversationParticipant.objects.filter(
        conversation=thread, user=operator_user
    ).exists()


@pytest.mark.django_db
def test_rejoining_an_unknown_conversation_is_not_found(auth_client):
    response = auth_client.post(_rejoin_url("11111111-1111-1111-1111-111111111111"))
    assert response.status_code == 404
    assert (
        response.json()["localizable_error"] == "error.404.chat_conversation_not_found"
    )


@pytest.mark.django_db
def test_rejoin_conversation_is_idempotent_at_the_service(user, thread):
    services.leave_conversation(conversation=thread, user=user)
    assert services.rejoin_conversation(conversation=thread, user=user) is True
    assert services.rejoin_conversation(conversation=thread, user=user) is False


@pytest.mark.django_db
def test_the_other_side_sees_the_leaver_back_in_the_thread(
    auth_client, user, other_user, thread
):
    auth_client.delete(f"{LIST_URL}/{thread.id}")
    theirs = _client_for(other_user)
    row = _rows(theirs)[0]
    assert [p for p in row["participants"] if p["user_id"] == str(user.id)][0][
        "left_at"
    ] is not None

    auth_client.post(_rejoin_url(thread.id))

    row = _rows(theirs)[0]
    assert [p for p in row["participants"] if p["user_id"] == str(user.id)][0][
        "left_at"
    ] is None
    # The counterpart's own row never moved.
    assert row["left_at"] is None


# ── The cost of the listing ──────────────────────────────────────────────


class TestQueryCount:
    """A left listing costs the same at two rows and at six.

    The same measurement the inbox carries (`test_inbox_search.TestQueryCount`)
    and for the same reason: `viewer_left_at` is a subquery per PAGE, and a
    listing that costs one more query per row is the shape that only shows up
    where the list is not three rows long.
    """

    @staticmethod
    def _measure(auth_client, user, params, rows, tag):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(rows):
            other = _user(f"marta{tag}{i}")
            conv = services.create_direct(owner=user, other_user_id=other.id)
            # From the OTHER side, so every row is unread and the same fixture
            # measures the `unread` chip as well.
            services.post_message(
                conversation=conv, sender=other, body="about the marta thing"
            )
            services.leave_conversation(conversation=conv, user=user)
        with CaptureQueriesContext(connection) as ctx:
            response = auth_client.get(LIST_URL, params)
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == rows
        # A page that came back empty would hold the count flat by measuring
        # nothing. Every row here is a real left thread with every per-page
        # projection on it: the departure stamp, the unread annotation, the
        # last-line projection and both prefetched participants.
        assert all(row["left_at"] is not None for row in items)
        assert all(row["unread_count"] == 1 for row in items)
        assert all(row["last_message"]["seq"] is not None for row in items)
        assert all(len(row["participants"]) == 2 for row in items)
        return len(ctx.captured_queries)

    @pytest.mark.parametrize(
        "params",
        [
            {"left": "true"},
            {"left": "true", "search": "marta"},
            {"left": "true", "unread": "true"},
        ],
        ids=["plain", "search", "unread"],
    )
    def test_the_page_costs_the_same_at_two_and_at_six(
        self, auth_client, user, params
    ):
        small = self._measure(auth_client, user, params, 2, "a")
        Conversation.objects.all().delete()
        large = self._measure(auth_client, user, params, 6, "b")
        assert small == large
