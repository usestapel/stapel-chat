"""Presence: a fact about the OTHER person's sockets, never about yours.

The defect these tests pin: the header said "На связи" whenever the READER's
own socket was up. Everything below asserts the replacement is derived from
the subject's own connections — that it flips down when their last socket
closes, that it survives a worker that never got to run a disconnect, that a
heartbeat does not become a write per pong, and that the boolean and the
last-seen both reach the wire.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_chat import presence, services
from stapel_chat.models import UserPresence


def _users():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return (
        User.objects.create_user(username="pa", email="pa@x.com", password="x"),
        User.objects.create_user(username="pb", email="pb@x.com", password="x"),
    )


# ── the core state machine ───────────────────────────────────────────────


def test_a_user_nobody_has_seen_is_offline_with_no_last_seen(db):
    """Not an error, and not a fabricated timestamp: three honest nulls."""
    a, _ = _users()
    assert presence.for_user(a.id) == {
        "online": False,
        "last_seen_at": None,
        "online_until": None,
    }


def test_connect_makes_them_online_and_disconnect_makes_them_not(db):
    a, _ = _users()

    assert presence.on_connect(a.id) is True
    assert presence.for_user(a.id)["online"] is True

    assert presence.on_disconnect(a.id) is True
    state = presence.for_user(a.id)
    assert state["online"] is False
    # And the header now has something to render instead of "online".
    assert state["last_seen_at"] is not None


def test_two_tabs_are_one_person_and_closing_one_changes_nothing(db):
    """Presence is per user, not per socket. This is the regression that a
    naive boolean flag would reintroduce the first time somebody opened the
    inbox and a thread at the same time."""
    a, _ = _users()
    presence.on_connect(a.id)
    # The second connect is not a second transition — nobody is told again.
    assert presence.on_connect(a.id) is False
    assert UserPresence.objects.get(pk=a.pk).connections == 2

    assert presence.on_disconnect(a.id) is False
    assert presence.for_user(a.id)["online"] is True

    assert presence.on_disconnect(a.id) is True
    assert presence.for_user(a.id)["online"] is False


def test_a_worker_killed_mid_socket_does_not_leave_them_online_forever(db):
    """The counter says a socket is open; the lease says nobody has renewed
    it. Online is the AND, so the lease wins and the answer is offline."""
    a, _ = _users()
    presence.on_connect(a.id)
    assert presence.for_user(a.id)["online"] is True

    # No disconnect ever ran — the process died. Age the lease past its end.
    row = UserPresence.objects.get(pk=a.pk)
    UserPresence.objects.filter(pk=a.pk).update(
        online_until=timezone.now() - timedelta(seconds=1)
    )
    assert row.connections == 1  # the counter still claims a live socket
    assert presence.for_user(a.id)["online"] is False


def test_a_disconnect_without_a_connect_cannot_drive_the_counter_negative(db):
    """A restarted worker runs disconnects for sockets it never counted. If
    that pushed the counter below zero, a real open tab could never bring it
    back to 1 and the user would be stranded offline."""
    a, _ = _users()
    presence.on_connect(a.id)
    presence.on_disconnect(a.id)
    presence.on_disconnect(a.id)  # the phantom
    assert UserPresence.objects.get(pk=a.pk).connections == 0

    presence.on_connect(a.id)
    assert presence.for_user(a.id)["online"] is True


# ── the write throttle ───────────────────────────────────────────────────


def test_a_heartbeat_is_not_a_write_per_pong(db, settings):
    settings.STAPEL_CHAT = {"PRESENCE_WRITE_THROTTLE_S": 30, "PRESENCE_TTL_S": 90}
    a, _ = _users()
    presence.on_connect(a.id)

    # The connect just wrote, so the very next touch is refused by the row.
    assert presence.touch(a.id) is False
    assert presence.touch(a.id) is False


def test_a_touch_past_the_throttle_writes_and_renews_the_lease(db, settings):
    settings.STAPEL_CHAT = {"PRESENCE_WRITE_THROTTLE_S": 30, "PRESENCE_TTL_S": 90}
    a, _ = _users()
    presence.on_connect(a.id)

    stale = timezone.now() - timedelta(seconds=31)
    UserPresence.objects.filter(pk=a.pk).update(last_seen_at=stale, online_until=stale)
    assert presence.for_user(a.id)["online"] is False  # lease had lapsed

    assert presence.touch(a.id) is True
    assert presence.for_user(a.id)["online"] is True


def test_a_zero_throttle_writes_every_time(db, settings):
    """The documented way to turn the rationing off, asserted rather than
    assumed — a throttle whose disable value silently still throttles is a
    knob that lies."""
    settings.STAPEL_CHAT = {"PRESENCE_WRITE_THROTTLE_S": 0, "PRESENCE_TTL_S": 90}
    a, _ = _users()
    presence.on_connect(a.id)
    assert presence.touch(a.id) is True


# ── the wire ─────────────────────────────────────────────────────────────


def test_a_transition_reaches_the_conversation_stream(db, monkeypatch):
    """`chat.presence.changed` on the stream the thread header is already
    subscribed to — presence needs no second subscription."""
    from stapel_chat import realtime

    sent = []
    monkeypatch.setattr(
        realtime, "_signal", lambda stream, type_, payload: sent.append(
            (stream, type_, payload)
        )
    )
    a, b = _users()
    conv = services.create_direct(owner=a, other_user_id=b.id)

    presence.on_connect(b.id)

    frames = [f for f in sent if f[1] == realtime.SIGNAL_PRESENCE]
    assert len(frames) == 1
    stream, _, payload = frames[0]
    assert stream == realtime.conversation_stream(conv.id)
    assert payload["user_id"] == str(b.id)
    assert payload["online"] is True
    assert payload["conversation_id"] == str(conv.id)


def test_going_away_announces_when_with_the_flip(db, monkeypatch):
    from stapel_chat import realtime

    sent = []
    monkeypatch.setattr(
        realtime, "_signal", lambda stream, type_, payload: sent.append(payload)
        if type_ == realtime.SIGNAL_PRESENCE
        else None,
    )
    a, b = _users()
    services.create_direct(owner=a, other_user_id=b.id)

    presence.on_connect(b.id)
    sent.clear()
    presence.on_disconnect(b.id)

    assert len(sent) == 1
    assert sent[0]["online"] is False
    # The offline header renders a time; it must not have to ask for it.
    assert sent[0]["last_seen_at"] is not None


def test_a_renewal_announces_nothing(db, monkeypatch):
    """Only flips travel. A touch on a socket that is already online tells
    every peer something they already know, at heartbeat frequency."""
    from stapel_chat import realtime

    sent = []
    monkeypatch.setattr(
        realtime,
        "_signal",
        lambda stream, type_, payload: sent.append(payload)
        if type_ == realtime.SIGNAL_PRESENCE
        else None,
    )
    a, b = _users()
    services.create_direct(owner=a, other_user_id=b.id)
    presence.on_connect(b.id)
    sent.clear()

    stale = timezone.now() - timedelta(seconds=120)
    UserPresence.objects.filter(pk=b.pk).update(last_seen_at=stale)
    presence.touch(b.id)
    assert sent == []


def test_the_fanout_bound_is_honoured(db, settings, monkeypatch):
    from stapel_chat import realtime

    settings.STAPEL_CHAT = {"PRESENCE_FANOUT_LIMIT": 2}
    sent = []
    monkeypatch.setattr(
        realtime,
        "_signal",
        lambda stream, type_, payload: sent.append(payload)
        if type_ == realtime.SIGNAL_PRESENCE
        else None,
    )
    a, b = _users()
    from django.contrib.auth import get_user_model

    User = get_user_model()
    for i in range(4):
        peer = User.objects.create_user(
            username=f"peer{i}", email=f"peer{i}@x.com", password="x"
        )
        services.create_direct(owner=peer, other_user_id=b.id)

    presence.on_connect(b.id)
    assert len(sent) == 2


def test_a_zero_fanout_limit_leaves_presence_a_rest_only_fact(db, settings, monkeypatch):
    from stapel_chat import realtime

    settings.STAPEL_CHAT = {"PRESENCE_FANOUT_LIMIT": 0}
    sent = []
    monkeypatch.setattr(
        realtime,
        "_signal",
        lambda stream, type_, payload: sent.append(payload)
        if type_ == realtime.SIGNAL_PRESENCE
        else None,
    )
    a, b = _users()
    services.create_direct(owner=a, other_user_id=b.id)
    presence.on_connect(b.id)
    assert sent == []
    # Still true over REST — the bound governs the announcement, not the fact.
    assert presence.for_user(b.id)["online"] is True


# ── REST: the header paints on first load ────────────────────────────────


def test_the_conversation_body_carries_each_participant_s_presence(
    db, api_client
):
    """No extra round trip and no derivation from the reader's socket: the
    boolean and the timestamp ride back on the participant."""
    a, b = _users()
    conv = services.create_direct(owner=a, other_user_id=b.id)
    presence.on_connect(b.id)

    api_client.force_authenticate(user=a)
    res = api_client.get(f"/chat/api/v1/conversations/{conv.id}")
    assert res.status_code == 200

    by_id = {p["user_id"]: p for p in res.json()["participants"]}
    assert by_id[str(b.id)]["online"] is True
    # The reader has no socket in this test and is honestly reported offline —
    # the old header would have called them "На связи" for asking.
    assert by_id[str(a.id)]["online"] is False
    assert by_id[str(a.id)]["last_seen_at"] is None


def test_after_they_go_the_body_says_when(db, api_client):
    a, b = _users()
    conv = services.create_direct(owner=a, other_user_id=b.id)
    presence.on_connect(b.id)
    presence.on_disconnect(b.id)

    api_client.force_authenticate(user=a)
    res = api_client.get(f"/chat/api/v1/conversations/{conv.id}")
    peer = next(
        p for p in res.json()["participants"] if p["user_id"] == str(b.id)
    )
    assert peer["online"] is False
    assert peer["last_seen_at"] is not None


def test_the_list_resolves_presence_for_the_whole_page_at_once(
    db, api_client, django_assert_num_queries
):
    """The batching rule the card resolver already follows. A fifty-row inbox
    that asked per row is how a list becomes a load-bearing outage."""
    a, b = _users()
    from django.contrib.auth import get_user_model

    User = get_user_model()
    for i in range(3):
        peer = User.objects.create_user(
            username=f"lp{i}", email=f"lp{i}@x.com", password="x"
        )
        services.create_direct(owner=a, other_user_id=peer.id)
    presence.on_connect(b.id)

    api_client.force_authenticate(user=a)
    res = api_client.get("/chat/api/v1/conversations")
    assert res.status_code == 200
    rows = res.json()["items"]
    assert len(rows) == 3
    for row in rows:
        for participant in row["participants"]:
            assert "online" in participant
            assert "last_seen_at" in participant


# ── the config check ─────────────────────────────────────────────────────


def test_a_throttle_at_or_above_the_lease_is_a_boot_failure(settings):
    from stapel_chat.checks import check_presence_windows

    settings.STAPEL_CHAT = {"PRESENCE_TTL_S": 30, "PRESENCE_WRITE_THROTTLE_S": 30}
    issues = check_presence_windows(None)
    assert [i.id for i in issues] == ["stapel_chat.E021"]


def test_the_default_windows_boot(settings):
    from stapel_chat.checks import check_presence_windows

    settings.STAPEL_CHAT = {}
    assert check_presence_windows(None) == []


@pytest.mark.parametrize(
    "override",
    [
        {"PRESENCE_TTL_S": 0},
        {"PRESENCE_TTL_S": "90"},
        {"PRESENCE_WRITE_THROTTLE_S": -1},
        {"PRESENCE_FANOUT_LIMIT": -1},
    ],
)
def test_a_nonsense_window_is_refused_at_the_door(settings, override):
    from stapel_chat.checks import check_presence_windows

    settings.STAPEL_CHAT = override
    assert [i.id for i in check_presence_windows(None)] == ["stapel_chat.E021"]


# ── the deadline on the wire (0.7.3) ─────────────────────────────────────
#
# The defect this section pins was found on a live stand, not in a test: a
# reader's header said "online" ninety seconds after the peer was gone, while
# the SERVER had already said offline. Nothing was broken in the server — the
# lease had expired exactly as designed — and that is the whole problem. A
# flip is announced from a disconnect; a lease running out announces nothing,
# because nothing happens. No socket closes, no row is written, there is no
# event to send. So a client told only `online: true` believes it forever.
#
# `online_until` is the fix, and it is a fix by DATA rather than by event: the
# reader is given the same deadline the server evaluates and reaches the same
# answer on its own clock.


def test_the_body_ships_the_deadline_so_a_reader_can_expire_it_itself(
    db, api_client
):
    a, b = _users()
    conv = services.create_direct(owner=a, other_user_id=b.id)
    presence.on_connect(b.id)

    api_client.force_authenticate(user=a)
    res = api_client.get(f"/chat/api/v1/conversations/{conv.id}")
    peer = next(p for p in res.json()["participants"] if p["user_id"] == str(b.id))
    assert peer["online"] is True
    assert peer["online_until"] is not None


def test_a_lease_that_expired_without_a_disconnect_reads_offline(db, api_client):
    """THE case. connections > 0 — the disconnect never ran — and the lease is
    in the past. The body must say offline, and must hand the reader the
    deadline that makes that answer checkable."""
    a, b = _users()
    conv = services.create_direct(owner=a, other_user_id=b.id)
    presence.on_connect(b.id)

    past = timezone.now() - timedelta(seconds=1)
    UserPresence.objects.filter(pk=b.pk).update(online_until=past)
    row = UserPresence.objects.get(pk=b.pk)
    assert row.connections > 0, "the socket was never cleanly closed"

    api_client.force_authenticate(user=a)
    res = api_client.get(f"/chat/api/v1/conversations/{conv.id}")
    peer = next(p for p in res.json()["participants"] if p["user_id"] == str(b.id))
    assert peer["online"] is False
    assert peer["online_until"] is not None  # and it is in the past


def test_the_reader_could_have_reached_that_answer_alone(db):
    """The property the client half depends on: the deadline the server hands
    out is the same one it evaluates. Given the body, a client with a clock
    needs nothing else — no poll, and no event for a non-happening."""
    a, b = _users()
    presence.on_connect(b.id)
    past = timezone.now() - timedelta(seconds=1)
    UserPresence.objects.filter(pk=b.pk).update(online_until=past)

    state = presence.for_user(b.id)
    assert state["online"] is False
    assert state["online_until"] == past
    # The server's own verdict is exactly "is the deadline in the future".
    assert (state["online_until"] > timezone.now()) is state["online"]


def test_an_online_frame_carries_its_own_expiry(db, monkeypatch):
    from stapel_chat import realtime

    sent = []
    monkeypatch.setattr(
        realtime,
        "_signal",
        lambda stream, type_, payload: sent.append(payload)
        if type_ == realtime.SIGNAL_PRESENCE
        else None,
    )
    a, b = _users()
    services.create_direct(owner=a, other_user_id=b.id)

    presence.on_connect(b.id)
    assert len(sent) == 1
    assert sent[0]["online"] is True
    # Self-limiting: the frame that says "online" also says how long that is
    # good for, so a client that never hears another frame still expires it.
    assert sent[0]["online_until"] is not None


def test_a_going_away_frame_ends_the_lease_in_the_frame_itself(db, monkeypatch):
    from stapel_chat import realtime

    sent = []
    monkeypatch.setattr(
        realtime,
        "_signal",
        lambda stream, type_, payload: sent.append(payload)
        if type_ == realtime.SIGNAL_PRESENCE
        else None,
    )
    a, b = _users()
    services.create_direct(owner=a, other_user_id=b.id)
    presence.on_connect(b.id)
    sent.clear()

    presence.on_disconnect(b.id)
    assert sent[0]["online"] is False
    assert sent[0]["online_until"] is not None


def test_the_deadline_moves_forward_on_a_renewal(db, settings):
    """A touch renews the lease, so a client's timer is pushed out by the next
    body it reads. Without this the deadline would be a countdown to a wrong
    answer on a socket that is very much alive."""
    settings.STAPEL_CHAT = {"PRESENCE_WRITE_THROTTLE_S": 30, "PRESENCE_TTL_S": 90}
    a, _ = _users()
    presence.on_connect(a.id)
    first = presence.for_user(a.id)["online_until"]

    stale = timezone.now() - timedelta(seconds=31)
    UserPresence.objects.filter(pk=a.pk).update(last_seen_at=stale)
    assert presence.touch(a.id) is True

    assert presence.for_user(a.id)["online_until"] > first
