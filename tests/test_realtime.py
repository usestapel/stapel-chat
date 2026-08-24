"""The conversation socket: live delivery, resume, writes, receipts, activity.

Driven through ``stapel_realtime.testing`` — the substrate's own harness, which
speaks the v1 envelope and swallows heartbeat noise, so these tests assert
chat's protocol rather than re-implementing a communicator.

Every frame here is an envelope: ``{"v": 1, "type": ..., "stream": ...,
"payload": {...}}`` with ``seq`` on journal frames only. The flat
``{"type": "message", "body": ...}`` shape of 0.2.x is gone.
"""
import pytest
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from stapel_realtime import envelope as wire
from stapel_realtime.testing import StreamClient
from channels.testing.websocket import WebsocketCommunicator

from stapel_chat import services
from stapel_chat.consumers import ChatConsumer, ChatInboxConsumer
from stapel_chat.routing import websocket_urlpatterns

pytestmark = pytest.mark.asyncio


class _InjectUser:
    """Stand-in for JWTAuthMiddleware (G14): stamps a fixed user on the scope."""

    def __init__(self, inner, user):
        self.inner = inner
        self.user = user

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope["user"] = self.user
        return await self.inner(scope, receive, send)


def _routed_app(user):
    return _InjectUser(URLRouter(websocket_urlpatterns), user)


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


async def _open(conv_id, user, path=None):
    """A connected StreamClient on the conversation socket."""
    comm = WebsocketCommunicator(
        ChatConsumer.as_asgi(), path or f"/ws/chat/{conv_id}"
    )
    comm.scope["url_route"] = {"kwargs": {"conversation_id": str(conv_id)}}
    comm.scope["user"] = user
    connected, code = await comm.connect()
    return StreamClient(comm, connected=connected, close_code=code)


async def _open_inbox(user):
    comm = WebsocketCommunicator(ChatInboxConsumer.as_asgi(), "/ws/chat/inbox")
    comm.scope["url_route"] = {"kwargs": {}}
    comm.scope["user"] = user
    connected, code = await comm.connect()
    return StreamClient(comm, connected=connected, close_code=code)


# ── the substrate's protocol, spoken by chat ─────────────────────────────


@pytest.mark.django_db(transaction=True)
async def test_live_message_arrives_as_a_v1_journal_frame():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    sock = await _open(conv.id, a)
    assert sock.connected
    await database_sync_to_async(services.post_message)(
        conversation=conv, sender=b, body="hi"
    )
    frame = await sock.receive(timeout=3)
    assert frame.type == wire.LIVE
    assert frame.seq == 1  # the JOURNAL cursor (rev_seq)
    assert frame.stream == f"chat:conv:{conv.id}"
    assert frame.payload["body"] == "hi"
    assert frame.payload["seq"] == 1  # the message's place in the thread
    assert frame.payload["rev_seq"] == 1
    assert frame.payload["deleted"] is False
    assert frame.payload["edited"] is False
    await sock.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_hello_replays_missed_rows_then_replay_done():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    await database_sync_to_async(_seed)(conv, b, 3)
    sock = await _open(conv.id, a)
    welcome = await sock.hello(last_seq=1)
    assert welcome.payload["server_seq"] == 3
    first = await sock.expect(wire.REPLAY)
    assert first.seq == 2
    second = await sock.expect(wire.REPLAY)
    assert second.seq == 3
    done = await sock.expect(wire.REPLAY_DONE)
    assert done.payload["up_to_seq"] == 3
    await sock.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_a_gap_wider_than_the_window_answers_resync_not_error():
    """A resume gap is an instruction to re-hydrate, not a refusal — so it is
    its own frame type and the socket stays open (substrate contract)."""
    from django.test import override_settings

    a, b, conv = await database_sync_to_async(_setup_direct)()
    await database_sync_to_async(_seed)(conv, b, 5)
    with override_settings(STAPEL_REALTIME={"MAX_REPLAY": 2, "ALLOWED_ORIGINS": []}):
        sock = await _open(conv.id, a)
        await sock.hello(last_seq=0)
        resync = await sock.expect(wire.RESYNC)
        assert resync.payload["gap"] == 5
        assert resync.payload["window"] == 2
        await sock.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_non_participant_is_refused_4403():
    a, b = await database_sync_to_async(_make_users)()
    conv = await database_sync_to_async(services.create_group)(owner=b)
    sock = await _open(conv.id, a)
    assert not sock.connected
    assert sock.close_code == 4403


@pytest.mark.django_db(transaction=True)
async def test_unauthenticated_is_refused_4401():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    sock = await _open(conv.id, None)
    assert not sock.connected
    assert sock.close_code == 4401


@pytest.mark.django_db(transaction=True)
async def test_routing_resolves_the_real_path_to_the_consumer():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    await database_sync_to_async(_seed)(conv, b, 2)
    comm = WebsocketCommunicator(_routed_app(a), f"/ws/chat/{conv.id}")
    connected, _ = await comm.connect()
    assert connected
    sock = StreamClient(comm)
    welcome = await sock.hello(last_seq=0)
    assert welcome.payload["server_seq"] == 2
    assert (await sock.expect(wire.REPLAY)).seq == 1
    assert (await sock.expect(wire.REPLAY)).seq == 2
    await sock.expect(wire.REPLAY_DONE)
    await comm.disconnect()


# ── writes over the socket ───────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
async def test_send_frame_persists_and_fans_back():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    sock = await _open(conv.id, a)
    await sock.send("send", {"body": "from socket", "client_msg_id": "c-1"})
    frame = await sock.expect(wire.LIVE, timeout=3)
    assert frame.payload["body"] == "from socket"
    assert frame.payload["sender_id"] == str(a.id)
    assert frame.payload["client_msg_id"] == "c-1"
    await sock.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_send_is_idempotent_by_client_msg_id():
    """Enter pressed once, socket dropped, client retried — one message."""
    from stapel_chat.models import Message

    a, b, conv = await database_sync_to_async(_setup_direct)()
    for _ in range(2):
        await database_sync_to_async(services.post_message)(
            conversation=conv, sender=a, body="only once", client_msg_id="dup"
        )
    count = await database_sync_to_async(
        Message.objects.filter(conversation=conv, client_msg_id="dup").count
    )()
    assert count == 1


@pytest.mark.django_db(transaction=True)
async def test_empty_send_answers_an_error_frame():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    sock = await _open(conv.id, a)
    await sock.send("send", {"body": "   "})
    err = await sock.expect(wire.ERROR)
    assert err.payload["code"] == "empty"
    await sock.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_edit_frame_redelivers_the_row_under_a_new_journal_seq():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    msg = await database_sync_to_async(services.post_message)(
        conversation=conv, sender=a, body="teh typo"
    )
    sock = await _open(conv.id, a)
    await sock.hello(last_seq=1)
    await sock.expect(wire.REPLAY_DONE)
    await sock.send("edit", {"message_id": str(msg.id), "body": "the typo"})
    frame = await sock.expect(wire.LIVE, timeout=3)
    # The journal cursor moved; the message's place in the thread did not.
    assert frame.seq == 2
    assert frame.payload["seq"] == 1
    assert frame.payload["rev_seq"] == 2
    assert frame.payload["body"] == "the typo"
    assert frame.payload["edited"] is True
    assert frame.payload["edited_at"] is not None
    await sock.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_delete_frame_delivers_a_tombstone_not_a_removal():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    msg = await database_sync_to_async(services.post_message)(
        conversation=conv, sender=a, body="regret", attachments=[{"key": "file/x", "type": "file"}]
    )
    sock = await _open(conv.id, a)
    await sock.hello(last_seq=1)
    await sock.expect(wire.REPLAY_DONE)
    await sock.send("delete", {"message_id": str(msg.id)})
    frame = await sock.expect(wire.LIVE, timeout=3)
    assert frame.payload["message_id"] == str(msg.id)  # the id still arrives
    assert frame.payload["seq"] == 1
    assert frame.payload["deleted"] is True
    assert frame.payload["body"] == ""
    assert frame.payload["attachments"] == []
    await sock.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_a_message_deleted_while_offline_arrives_in_the_replay():
    """The point of rev_seq. The client acknowledged seq 1 and went away; the
    delete must still reach it, and anchored on seq it never could."""
    a, b, conv = await database_sync_to_async(_setup_direct)()
    msg = await database_sync_to_async(services.post_message)(
        conversation=conv, sender=b, body="gone soon"
    )
    await database_sync_to_async(services.delete_message)(message=msg, actor=b)
    sock = await _open(conv.id, a)
    await sock.hello(last_seq=1)  # "I already have seq 1"
    replay = await sock.expect(wire.REPLAY)
    assert replay.seq == 2  # the tombstone's rev_seq
    assert replay.payload["message_id"] == str(msg.id)
    assert replay.payload["deleted"] is True
    await sock.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_only_the_author_may_edit_or_delete():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    msg = await database_sync_to_async(services.post_message)(
        conversation=conv, sender=b, body="not yours"
    )
    sock = await _open(conv.id, a)
    await sock.send("edit", {"message_id": str(msg.id), "body": "hijacked"})
    err = await sock.expect(wire.ERROR)
    assert err.payload["code"] == "not_author"
    await sock.send("delete", {"message_id": str(msg.id)})
    err = await sock.expect(wire.ERROR)
    assert err.payload["code"] == "not_author"
    await sock.communicator.disconnect()


# ── receipts and activity: ephemeral signals ─────────────────────────────


@pytest.mark.django_db(transaction=True)
async def test_read_receipt_reaches_the_other_participant():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    await database_sync_to_async(_seed)(conv, a, 1)
    watcher = await _open(conv.id, a)
    reader = await _open(conv.id, b)
    await reader.send("read", {"upto_seq": 1})
    frame = await watcher.receive(timeout=3)
    assert frame.type == "chat.read"
    assert frame.payload["user_id"] == str(b.id)
    assert frame.payload["last_read_seq"] == 1
    assert frame.seq is None  # ephemeral: no seq, structurally not journal
    await watcher.communicator.disconnect()
    await reader.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_delivery_receipt_is_a_separate_fact_from_read():
    from stapel_chat.models import ConversationParticipant

    a, b, conv = await database_sync_to_async(_setup_direct)()
    await database_sync_to_async(_seed)(conv, a, 2)
    reader = await _open(conv.id, b)
    await reader.send("delivered", {"upto_seq": 2})
    frame = await reader.receive(timeout=3)
    assert frame.type == "chat.delivered"
    assert frame.payload["last_delivered_seq"] == 2
    row = await database_sync_to_async(
        lambda: ConversationParticipant.objects.get(conversation=conv, user=b)
    )()
    assert row.last_delivered_seq == 2
    assert row.last_read_seq == 0  # holding it is not reading it
    await reader.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_typing_is_an_ephemeral_signal_with_a_ttl():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    watcher = await _open(conv.id, a)
    typist = await _open(conv.id, b)
    await typist.send("activity", {"state": "typing"})
    frame = await watcher.receive(timeout=3)
    assert frame.type == "chat.activity"
    assert frame.payload["state"] == "typing"
    assert frame.payload["ttl_s"] > 0
    assert frame.seq is None
    await watcher.communicator.disconnect()
    await typist.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_an_unregistered_activity_state_is_refused():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    sock = await _open(conv.id, a)
    await sock.send("activity", {"state": "choosing_sticker"})
    err = await sock.expect(wire.ERROR)
    assert err.payload["code"] == "unknown_activity"
    await sock.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_a_registered_activity_state_is_accepted():
    """The registry is OPEN: the state the owner already named is a settings
    line, not a release."""
    from stapel_chat.activity import register_activity_state, reset_activity_states

    a, b, conv = await database_sync_to_async(_setup_direct)()
    register_activity_state("choosing_sticker", {"ttl_s": 12})
    try:
        watcher = await _open(conv.id, a)
        typist = await _open(conv.id, b)
        await typist.send("activity", {"state": "choosing_sticker"})
        frame = await watcher.receive(timeout=3)
        assert frame.payload["state"] == "choosing_sticker"
        assert frame.payload["ttl_s"] == 12
        await watcher.communicator.disconnect()
        await typist.communicator.disconnect()
    finally:
        reset_activity_states()


# ── the inbox socket: a conversation list that does not poll ─────────────


@pytest.mark.django_db(transaction=True)
async def test_inbox_socket_receives_a_message_from_any_conversation():
    a, b, conv = await database_sync_to_async(_setup_direct)()
    inbox = await _open_inbox(a)
    assert inbox.connected
    await database_sync_to_async(services.post_message)(
        conversation=conv, sender=b, body="ping"
    )
    frame = await inbox.receive(timeout=3)
    assert frame.type == "chat.inbox"
    assert frame.stream == f"chat:user:{a.id}"
    assert frame.payload["conversation_id"] == str(conv.id)
    assert frame.payload["message"]["body"] == "ping"
    await inbox.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_inbox_stream_key_comes_from_the_scope_not_the_url():
    """There is no user segment in the route, so there is nothing to tamper
    with: whoever connects gets exactly their own inbox."""
    a, b, conv = await database_sync_to_async(_setup_direct)()
    inbox = await _open_inbox(b)
    assert inbox.connected
    frame = await inbox.hello()
    assert frame.payload.get("ephemeral") is True
    await inbox.communicator.disconnect()


@pytest.mark.django_db(transaction=True)
async def test_anonymous_inbox_is_refused():
    inbox = await _open_inbox(None)
    assert not inbox.connected
    assert inbox.close_code == 4401
