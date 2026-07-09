"""ChatConsumer: live delivery, resume-by-seq replay, resync, authz.

Driven through Channels' WebsocketCommunicator against the consumer directly
(scope["user"] injected the way JWTAuthMiddleware would). Transaction DB so the
service layer's ``transaction.on_commit`` fan-out actually fires.
"""
import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator

from stapel_chat import services
from stapel_chat.consumers import ChatConsumer

pytestmark = pytest.mark.asyncio


def _make_users():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    a = User.objects.create_user(username="a", email="a@x.com", password="x")
    b = User.objects.create_user(username="b", email="b@x.com", password="x")
    return a, b


def _setup_direct():
    a, b = _make_users()
    conv = services.create_direct(owner=a, other_user_id=b.id)
    return a, b, conv


def _seed(conv, sender, n):
    for i in range(n):
        services.post_message(conversation=conv, sender=sender, body=f"m{i}")


def _comm(conv_id, user):
    comm = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv_id}/")
    comm.scope["url_route"] = {"kwargs": {"conversation_id": str(conv_id)}}
    comm.scope["user"] = user
    return comm


@pytest.mark.django_db(transaction=True)
async def test_live_message_is_delivered():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    comm = _comm(conv.id, a)
    connected, _ = await comm.connect()
    assert connected
    await database_sync_to_async(services.post_message)(
        conversation=conv, sender=b, body="hi"
    )
    frame = await comm.receive_json_from(timeout=3)
    assert frame["type"] == "message"
    assert frame["seq"] == 1
    assert frame["body"] == "hi"
    await comm.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_inbound_send_frame_persists_and_fans_out():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    comm = _comm(conv.id, a)
    assert (await comm.connect())[0]
    await comm.send_json_to({"type": "send", "body": "from socket"})
    frame = await comm.receive_json_from(timeout=3)
    assert frame["type"] == "message"
    assert frame["body"] == "from socket"
    assert frame["sender_id"] == str(a.id)
    await comm.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_reconnect_replays_missed_by_seq():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    await database_sync_to_async(_seed)(conv, b, 3)  # seq 1,2,3
    comm = _comm(conv.id, a)
    assert (await comm.connect())[0]
    await comm.send_json_to({"type": "hello", "last_seq": 1})
    welcome = await comm.receive_json_from(timeout=3)
    assert welcome["type"] == "welcome" and welcome["server_seq"] == 3
    assert (await comm.receive_json_from())["seq"] == 2
    assert (await comm.receive_json_from())["seq"] == 3
    done = await comm.receive_json_from()
    assert done["type"] == "replay_done" and done["up_to_seq"] == 3
    await comm.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_replay_deduplicates_against_live_frames():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    await database_sync_to_async(_seed)(conv, b, 2)  # seq 1,2
    comm = _comm(conv.id, a)
    assert (await comm.connect())[0]
    # Client already has up to seq 2 -> replay sends nothing, straight to done.
    await comm.send_json_to({"type": "hello", "last_seq": 2})
    assert (await comm.receive_json_from())["type"] == "welcome"
    assert (await comm.receive_json_from())["type"] == "replay_done"
    await comm.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_resync_when_gap_exceeds_window(monkeypatch):
    monkeypatch.setattr("stapel_chat.consumers.REPLAY_LIMIT", 2)
    a, b, conv = await database_sync_to_async(_setup_direct)()
    await database_sync_to_async(_seed)(conv, b, 5)
    comm = _comm(conv.id, a)
    assert (await comm.connect())[0]
    await comm.send_json_to({"type": "hello", "last_seq": 0})
    assert (await comm.receive_json_from())["type"] == "welcome"
    err = await comm.receive_json_from()
    assert err["type"] == "error" and err["code"] == "resync"
    await comm.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_non_participant_is_rejected():
    a, b = await database_sync_to_async(_make_users)()
    conv = await database_sync_to_async(services.create_group)(owner=b)
    comm = _comm(conv.id, a)  # `a` is not a participant
    connected, code = await comm.connect()
    assert not connected
    assert code == 4403


@pytest.mark.django_db(transaction=True)
async def test_unauthenticated_is_rejected():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    comm = WebsocketCommunicator(ChatConsumer.as_asgi(), f"/ws/chat/{conv.id}/")
    comm.scope["url_route"] = {"kwargs": {"conversation_id": str(conv.id)}}
    comm.scope["user"] = None
    connected, code = await comm.connect()
    assert not connected
    assert code == 4401
