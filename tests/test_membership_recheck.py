"""Membership is re-checked on every write and revoked on every removal
(CH-2); send idempotency is per sender (CH-4); a malformed write frame gets an
error frame instead of killing the socket (CH-5)."""
import pytest
from channels.db import database_sync_to_async
from channels.testing.websocket import WebsocketCommunicator
from django.test import override_settings
from stapel_core.comm import emit
from stapel_core.django.users.models import User
from stapel_realtime import envelope as wire
from stapel_realtime.close_codes import CLOSE_FORBIDDEN, CLOSE_REVOKED
from stapel_realtime.testing import StreamClient

from stapel_chat import services
from stapel_chat.consumers import ChatConsumer
from stapel_chat.models import ConversationParticipant, Message, MessageKind


def _make_users():
    from django.contrib.auth import get_user_model

    U = get_user_model()
    return (
        U.objects.create_user(username="a", email="a@x.com", password="x"),
        U.objects.create_user(username="b", email="b@x.com", password="x"),
    )


def _setup_direct():
    a, b = _make_users()
    return a, b, services.create_direct(owner=a, other_user_id=b.id)


async def _open(conv_id, user):
    comm = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv_id}")
    comm.scope["url_route"] = {"kwargs": {"conversation_id": str(conv_id)}}
    comm.scope["user"] = user
    connected, code = await comm.connect()
    return StreamClient(comm, connected=connected, close_code=code)


async def _next_non_presence(sock, timeout=3):
    from stapel_chat.realtime import SIGNAL_PRESENCE

    while True:
        frame = await sock.receive(timeout=timeout)
        if frame.type != SIGNAL_PRESENCE:
            return frame


def _remove_quietly(conv, user):
    """The admin inline / a raw ORM delete: no leave(), no revoke() call."""
    ConversationParticipant.objects.filter(conversation=conv, user=user).delete()


# ── CH-2: write frames re-check membership ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_write_after_removal_is_refused_even_without_a_revoke(monkeypatch):
    from stapel_chat import realtime

    # Take the revoke away: only the per-write re-check can stop this send.
    monkeypatch.setattr(realtime, "revoke_participant", lambda *a, **k: None)
    a, b, conv = await database_sync_to_async(_setup_direct)()
    with override_settings(STAPEL_REALTIME={"AUTHORIZE_CACHE_S": 300, "ALLOWED_ORIGINS": []}):
        sock = await _open(conv.id, a)
        assert sock.connected
        await database_sync_to_async(_remove_quietly)(conv, a)
        await sock.send("send", {"body": "after removal", "client_msg_id": "r-1"})
        err = await _next_non_presence(sock)
        assert err.type == wire.ERROR
        assert err.payload["code"] == wire.ERROR_UNAUTHORIZED
        assert await sock.wait_closed(timeout=3) == CLOSE_FORBIDDEN
    assert not await database_sync_to_async(
        Message.objects.filter(conversation=conv, body="after removal").exists
    )()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_removing_a_participant_by_any_path_revokes_the_open_socket():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    with override_settings(STAPEL_REALTIME={"AUTHORIZE_CACHE_S": 300, "ALLOWED_ORIGINS": []}):
        sock = await _open(conv.id, a)
        assert sock.connected
        await database_sync_to_async(_remove_quietly)(conv, a)
        kick = await _next_non_presence(sock)
        assert kick.type == wire.KICK
        assert kick.payload["reason"] == "removed_from_conversation"
        assert await sock.wait_closed(timeout=3) == CLOSE_REVOKED


# ── CH-4: idempotency per sender ─────────────────────────────────────────


@pytest.mark.django_db
def test_two_participants_with_the_same_client_id_both_get_their_message():
    a, b, conv = _setup_direct()
    first = services.post_message(
        conversation=conv, sender=a, body="from a", client_msg_id="tmp-1"
    )
    second = services.post_message(
        conversation=conv, sender=b, body="from b", client_msg_id="tmp-1"
    )
    assert second.pk != first.pk
    assert second.body == "from b"
    assert second.sender_id == b.pk
    assert Message.objects.filter(conversation=conv, client_msg_id="tmp-1").count() == 2


@pytest.mark.django_db
def test_one_senders_retry_is_still_one_message():
    a, b, conv = _setup_direct()
    first = services.post_message(
        conversation=conv, sender=a, body="once", client_msg_id="tmp-1"
    )
    again = services.post_message(
        conversation=conv, sender=a, body="once", client_msg_id="tmp-1"
    )
    assert again.pk == first.pk
    assert Message.objects.filter(conversation=conv, client_msg_id="tmp-1").count() == 1


@pytest.mark.django_db
def test_a_system_line_key_is_per_conversation():
    a, b, conv = _setup_direct()
    services.post_message(conversation=conv, sender=a, body="mine", client_msg_id="k")
    one = services.post_system_message(conv.pk, "line", client_msg_id="k")
    two = services.post_system_message(conv.pk, "line", client_msg_id="k")
    assert one.pk == two.pk
    assert one.kind == MessageKind.SYSTEM
    assert Message.objects.filter(conversation=conv, client_msg_id="k").count() == 2


@pytest.mark.django_db
def test_a_guest_merge_does_not_trip_the_per_sender_key(user, other_user):
    guest = User.create_anonymous_user()
    conv = services.create_group(owner=other_user, participant_ids=[guest.id, user.id])
    services.post_message(conversation=conv, sender=guest, body="g", client_msg_id="tmp-1")
    services.post_message(conversation=conv, sender=user, body="u", client_msg_id="tmp-1")
    emit(
        "user.merged",
        {
            "from_user_id": str(guest.id),
            "into_user_id": str(user.id),
            "reason": "anonymous_promotion",
        },
    )
    bodies = set(
        Message.objects.filter(conversation=conv, sender=user).values_list("body", flat=True)
    )
    assert bodies == {"g", "u"}
    assert (
        Message.objects.filter(conversation=conv, sender=user, client_msg_id="tmp-1").count()
        == 1
    )


# ── CH-5: malformed write frames answer an error frame ───────────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "frame_type,payload",
    [
        ("send", {"body": 5}),
        ("send", {"body": "x", "attachments": 5}),
        ("send", {"body": "x", "reply_to": "not-a-uuid"}),
        ("edit", {"message_id": "x", "body": "y"}),
        ("delete", {"message_id": 7}),
    ],
)
async def test_a_malformed_field_answers_bad_envelope_and_keeps_the_socket(
    frame_type, payload
):
    a, b, conv = await database_sync_to_async(_setup_direct)()
    sock = await _open(conv.id, a)
    await sock.send(frame_type, payload)
    err = await _next_non_presence(sock)
    assert err.type == wire.ERROR
    assert err.payload["code"] == wire.ERROR_BAD_ENVELOPE
    # Still open: a valid send goes through on the same socket.
    await sock.send("send", {"body": "still here"})
    live = await _next_non_presence(sock)
    assert live.type == wire.LIVE
    assert live.payload["body"] == "still here"
    await sock.communicator.disconnect()
