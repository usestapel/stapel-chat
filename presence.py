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

**Presence is told to accounts, not to sessions.** A storefront mints a
*guest* the moment somebody taps "message the seller" — a real row that
passes ``IsAuthenticated`` while nobody has signed up for anything. Presence
is a fact about a person's day ("last seen 38 minutes ago"), and handing it
to a session that has not named itself hands it to the open internet, one
tap at a time. So both paths ask :func:`is_account` and not
``is_authenticated``: the REST read ships the offline default to a guest
(:func:`stapel_chat.services.presence_for`), and :func:`announce` skips a
conversation a guest can read. The account on the other side keeps both.
``PRESENCE_REQUIRES_ACCOUNT=False`` restores the old answer as something a
deployment states rather than inherits. A deployment whose user model has no
guests is unaffected — the field the test reads does not exist there.
"""
from __future__ import annotations

import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

#: What a snapshot answers for a user nobody has ever seen connect.
UNKNOWN = {"online": False, "last_seen_at": None, "online_until": None}


def _ttl() -> int:
    from .conf import chat_settings

    return max(1, int(chat_settings.PRESENCE_TTL_S))


def _throttle() -> int:
    from .conf import chat_settings

    return max(0, int(chat_settings.PRESENCE_WRITE_THROTTLE_S))


def is_account(user) -> bool:
    """Whether this caller is a person with an account, not a guest session.

    Two different questions wear the same word. Django's ``AnonymousUser``
    answers ``is_authenticated=False`` — the unsigned browser. A **guest** is
    a stored user minted from an action handler before anybody registered
    (``stapel_core``'s ``User.create_anonymous_user``): it answers
    ``is_authenticated=True`` and carries ``is_anonymous=True``. Every
    ``IsAuthenticated`` view in the fleet lets it through, which is the point
    of guests; what it must not do is make somebody else's presence readable.

    On a user model without guests (plain Django), ``is_anonymous`` is the
    inherited property — ``False`` for a signed-in user — so this is exactly
    ``is_authenticated`` there and nothing changes.
    """
    if user is None:
        return False
    if not getattr(user, "is_authenticated", False):
        return False
    return not bool(getattr(user, "is_anonymous", False))


def readable_by(user) -> bool:
    """Whether presence may be disclosed to this caller at all."""
    from .conf import chat_settings

    if not chat_settings.PRESENCE_REQUIRES_ACCOUNT:
        return True
    return is_account(user)


def _guest_ids(user_ids) -> set:
    """Which of these user ids are guests — empty where guests don't exist.

    Asked of the user model, once per fan-out, and only about the ids already
    in hand. ``FieldDoesNotExist`` is the answer for a deployment whose user
    model has no such notion: no guests, nothing to withhold from.
    """
    from django.contrib.auth import get_user_model
    from django.core.exceptions import FieldDoesNotExist

    wanted = [uid for uid in user_ids if uid]
    if not wanted:
        return set()
    User = get_user_model()
    try:
        User._meta.get_field("is_anonymous")
    except FieldDoesNotExist:
        return set()
    return {
        str(uid)
        for uid in User.objects.filter(
            id__in=wanted, is_anonymous=True
        ).values_list("id", flat=True)
    }


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
            # The lease deadline, shipped so a reader can reach the same
            # answer this function just computed WITHOUT asking again. See
            # the module docstring: a lease running out is a silent
            # transition, and a client that cannot see the deadline has no
            # way to notice one.
            "online_until": row.online_until,
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
    deadline = now + timedelta(seconds=_ttl())
    row, created = UserPresence.objects.get_or_create(
        user_id=user_id,
        defaults={"connections": 0, "last_seen_at": now, "online_until": now},
    )
    was_online = False if created else is_online(row, now)
    UserPresence.objects.filter(pk=row.pk).update(
        connections=F("connections") + 1,
        online_until=deadline,
        last_seen_at=now,
    )
    if not was_online:
        announce(user_id, online=True, last_seen_at=now, online_until=deadline)
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
    announce(user_id, online=False, last_seen_at=now, online_until=now)
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


def announce(user_id, *, online: bool, last_seen_at=None, online_until=None) -> int:
    """Tell every thread this user takes part in that their presence flipped.

    Bounded by ``PRESENCE_FANOUT_LIMIT``, newest threads first: a transition
    is worth telling the conversations somebody might be looking at, and a
    user with ten thousand dormant threads must not turn one socket close into
    ten thousand signals. A thread past the bound repaints from the
    participant's presence on its next REST read, which every open costs
    anyway.

    A thread a **guest** takes part in is skipped entirely (see the module
    docstring). The frame is addressed to a stream, not to a person, so there
    is no per-recipient filter to apply at delivery: the choice is between
    telling the guest and not sending it. The account on the other side keeps
    presence over REST, where the reader is known.
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
    if conversation_ids and chat_settings.PRESENCE_REQUIRES_ACCOUNT:
        others = list(
            ConversationParticipant.objects.filter(
                conversation_id__in=conversation_ids
            )
            .exclude(user_id=user_id)
            .values_list("conversation_id", "user_id")
        )
        guests = _guest_ids({uid for _, uid in others})
        if guests:
            listening = {
                str(cid) for cid, uid in others if str(uid) in guests
            }
            conversation_ids = [
                cid for cid in conversation_ids if str(cid) not in listening
            ]
    for conversation_id in conversation_ids:
        broadcast_presence(
            conversation_id,
            user_id,
            online=online,
            last_seen_at=last_seen_at,
            online_until=online_until,
        )
    return len(conversation_ids)


__all__ = [
    "UNKNOWN",
    "announce",
    "for_user",
    "is_account",
    "is_online",
    "on_connect",
    "on_disconnect",
    "readable_by",
    "snapshot",
    "touch",
]
