"""Who is actually there — presence as a fact about the OTHER person.

The product finding that routed this here: a classified chat drew "На связи"
(*"connected"*) in the thread header whenever **the reader's own** socket was
up. It read as "the seller is online" and it was never that; it was the
browser saying it could still reach the server. Two people sitting in a dead
thread were both told the other one was there.

So presence is a server-side fact about a *user*, derived from that user's own
connections, and the client is forbidden from deriving it from its own socket.
The reader's transport health is a different indicator that stays where it
was (``stapel-realtime``'s degradation states); this module answers only
"is **they** there, and if not, when were they last".

**Two facts, and both are needed.** A live connection count gives an
immediate transition — the last tab closes and the peer sees it at once. A
lease (``online_until``) gives crash safety — a worker killed mid-socket
leaves a counter that never decrements, and without a lease that user is
online forever. Online is the AND of the two: at least one connection is
believed open **and** the lease has not run out. Whichever fact is wrong, the
answer degrades to "offline", which is the honest direction to fail in — a
false "online" is the defect this module exists to delete.

**The write throttle is why this can live on a heartbeat.** Every inbound
frame is evidence of life, and a socket that pongs every twenty seconds must
not be twenty seconds of database writes. :func:`touch` writes only when the
stored ``last_seen_at`` is already older than ``PRESENCE_WRITE_THROTTLE_S``,
in one conditional UPDATE — no read, no race to lose. The consumers hold a
second, in-process guard so a throttled touch does not even cost a thread hop.
The throttle must stay comfortably below the lease (30 s under 90 s by
default) or a busy socket would let its own lease lapse.

**Transitions are announced, heartbeats are not.** ``chat.presence.changed``
rides the conversation streams the user takes part in — the stream a thread
header is already subscribed to, so the header updates without a second
subscription — and only when the boolean actually flips. A renewal that
changes nothing tells nobody anything.
"""
from __future__ import annotations

import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

#: What a snapshot answers for a user nobody has ever seen connect.
UNKNOWN = {"online": False, "last_seen_at": None}


def _ttl() -> int:
    from .conf import chat_settings

    return max(1, int(chat_settings.PRESENCE_TTL_S))


def _throttle() -> int:
    from .conf import chat_settings

    return max(0, int(chat_settings.PRESENCE_WRITE_THROTTLE_S))


def is_online(row, now=None) -> bool:
    """Online == a connection is believed open AND the lease still stands."""
    from django.utils import timezone

    if row is None:
        return False
    now = now or timezone.now()
    return bool(
        (row.connections or 0) > 0
        and row.online_until is not None
        and row.online_until > now
    )


def snapshot(user_ids) -> dict:
    """``[user_id, …] -> {user_id: {"online", "last_seen_at"}}``.

    One query for a whole page of participants. A user with no row has never
    connected since this deployment started tracking, which is reported as
    offline with no last-seen rather than as an absence the caller must
    handle.
    """
    from django.utils import timezone

    from .models import UserPresence

    wanted = [str(uid) for uid in user_ids if uid]
    if not wanted:
        return {}
    now = timezone.now()
    out = {uid: dict(UNKNOWN) for uid in wanted}
    for row in UserPresence.objects.filter(user_id__in=wanted):
        out[str(row.user_id)] = {
            "online": is_online(row, now),
            "last_seen_at": row.last_seen_at,
        }
    return out


def for_user(user_id) -> dict:
    """One user's presence — :func:`snapshot` for a single id."""
    return snapshot([user_id]).get(str(user_id), dict(UNKNOWN))


# ── transitions ──────────────────────────────────────────────────────────


def on_connect(user_id) -> bool:
    """A socket opened for this user. Returns whether they became online.

    The row is created on first sight; the counter and the lease move in one
    UPDATE so a second connection racing the first cannot lose an increment.
    """
    from django.db.models import F
    from django.utils import timezone

    from .models import UserPresence

    now = timezone.now()
    row, created = UserPresence.objects.get_or_create(
        user_id=user_id,
        defaults={"connections": 0, "last_seen_at": now, "online_until": now},
    )
    was_online = False if created else is_online(row, now)
    UserPresence.objects.filter(pk=row.pk).update(
        connections=F("connections") + 1,
        online_until=now + timedelta(seconds=_ttl()),
        last_seen_at=now,
    )
    if not was_online:
        announce(user_id, online=True, last_seen_at=now)
        return True
    return False


def on_disconnect(user_id) -> bool:
    """A socket closed for this user. Returns whether they became offline.

    The lease is ended explicitly when the last connection goes, so the peer
    learns immediately rather than at lease expiry. `Greatest` floors the
    counter at zero: a disconnect whose connect was never recorded (a worker
    restarted under the socket) must not drive it negative and strand the
    user online.
    """
    from django.db.models import F, Value
    from django.db.models.functions import Greatest
    from django.utils import timezone

    from .models import UserPresence

    now = timezone.now()
    updated = UserPresence.objects.filter(user_id=user_id).update(
        connections=Greatest(F("connections") - 1, Value(0)),
        last_seen_at=now,
    )
    if not updated:
        return False
    row = UserPresence.objects.filter(user_id=user_id).first()
    if row is None or (row.connections or 0) > 0:
        return False
    UserPresence.objects.filter(pk=row.pk).update(online_until=now)
    announce(user_id, online=False, last_seen_at=now)
    return True


def touch(user_id) -> bool:
    """Evidence of life — renew the lease and last-seen, at most once per
    ``PRESENCE_WRITE_THROTTLE_S``. Returns whether it wrote.

    One conditional UPDATE, no read: the row filters itself out while it is
    fresh, so concurrent touches from three tabs cost one write between them.
    This never flips the boolean (a touch happens on a live socket, which is
    already online) and therefore never announces.
    """
    from django.db.models import Q
    from django.utils import timezone

    from .models import UserPresence

    now = timezone.now()
    cutoff = now - timedelta(seconds=_throttle())
    return bool(
        UserPresence.objects.filter(user_id=user_id)
        .filter(Q(last_seen_at__lt=cutoff) | Q(last_seen_at__isnull=True))
        .update(last_seen_at=now, online_until=now + timedelta(seconds=_ttl()))
    )


# ── fan-out ──────────────────────────────────────────────────────────────


def announce(user_id, *, online: bool, last_seen_at=None) -> int:
    """Tell every thread this user takes part in that their presence flipped.

    Bounded by ``PRESENCE_FANOUT_LIMIT``, newest threads first: a transition
    is worth telling the conversations somebody might be looking at, and a
    user with ten thousand dormant threads must not turn one socket close into
    ten thousand signals. A thread past the bound repaints from the
    participant's presence on its next REST read, which every open costs
    anyway.
    """
    from .conf import chat_settings
    from .models import ConversationParticipant
    from .realtime import broadcast_presence

    limit = max(0, int(chat_settings.PRESENCE_FANOUT_LIMIT))
    if not limit:
        return 0
    conversation_ids = list(
        ConversationParticipant.objects.filter(user_id=user_id)
        .order_by("-conversation__updated_at")
        .values_list("conversation_id", flat=True)[:limit]
    )
    for conversation_id in conversation_ids:
        broadcast_presence(
            conversation_id, user_id, online=online, last_seen_at=last_seen_at
        )
    return len(conversation_ids)


__all__ = [
    "UNKNOWN",
    "announce",
    "for_user",
    "is_online",
    "on_connect",
    "on_disconnect",
    "snapshot",
    "touch",
]
