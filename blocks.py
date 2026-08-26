"""Block enforcement on both doors a block has to hold: opening a thread, and
sending into one.

A user-to-user block is **not** a chat concept and this module does not own
one. stapel-profiles has owned it since 0.4.x — ``UserRelationship`` with a
``blocked`` status and a REST surface to set it. What the fleet has never had
is a server that consults it: stapel-classified made the block hold at *new
conversation* creation, and said so plainly — *"a block that only stops NEW
conversations is half a block; the send path is chat's"*. 0.6.0 built that
half. 0.6.1 takes the other one over as well, so a composite no longer keeps a
pre-creation door of its own.

**The two doors are not symmetrical, and the asymmetry is the design.** A
block refuses a thread that does not exist yet, and never one that does.
Creating is a write; *returning* the pair's existing thread is a read of
history, and across this fleet a block never deletes history. Both parties go
on seeing what was already said; neither can add to it, because the send path
still refuses. Collapsing that either way is the defect — refuse-always takes
a conversation off two people as a side effect of one tap, allow-always
reopens the door this exists to close.

The provider is reached **by name, never imported**:
``profiles.relationships``, ``{"pairs": [[a, b], …]} -> {"blocked": [[a, b],
…]}``, blocked in EITHER direction. The name is a settings key, so a
deployment repoints it without a fork, and no import edge is created between
two modules that must stay independently deployable.

Three rules, and none of them is negotiable:

1. **A refusal never discloses the block.** Direction is not reported to the
   caller and the error key does not name a block at all — telling the blocked
   party "they blocked you" turns a quiet boundary into a notification, which
   is the opposite of what a block is for. The refusal a sender sees is the
   same one they would see if the thread had been closed to them for any other
   reason.
2. **A provider that is present and FAILS answers a server error, never
   "allowed".** An outage is not consent. This is stapel-classified's
   ``BlockCheckUnavailable`` → 503 precedent, kept identical so the two
   modules cannot disagree about what an unreachable block store means.
3. **Never degrade silently.** ``BLOCK_ENFORCEMENT`` is an axis with three
   states and ``manage.py check`` says at every boot which one this deployment
   is in (``stapel_chat.W003`` / ``E017`` / ``W004``). "Blocks are not
   enforced here" is a sentence an operator reads, not a thing they discover.

**Which conversations.** Direct threads only. A block is a fact between two
people; a group thread is a room somebody else convened, and silently dropping
one member's messages out of it would be a different product with a different
answer (and a UI obligation this module cannot meet). A support thread is
never checked at all: an operator is not a peer, and letting a customer mute
support by blocking an agent would be a denial-of-service on the help desk.
Both exclusions are asserted by tests, so neither can drift into an accident.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ENFORCEMENT_AUTO = "auto"
ENFORCEMENT_REQUIRED = "required"
ENFORCEMENT_OFF = "off"
ENFORCEMENT_MODES = (ENFORCEMENT_AUTO, ENFORCEMENT_REQUIRED, ENFORCEMENT_OFF)


class BlockCheckUnavailable(Exception):
    """The block store is configured and could not be asked (→ 503).

    Deliberately NOT a subclass of the module's ``ChatError`` refusals: this
    is not "you may not", it is "nobody can currently say", and the two must
    never map to the same status. A 403 here would tell a sender they are
    blocked when in fact a service was down.
    """


def provider_unreachable_reason() -> str:
    """Why the block provider cannot be called, or ``""`` when it can."""
    from stapel_core.comm import function_unreachable_reason

    from .conf import chat_settings

    name = (chat_settings.BLOCK_FUNCTION or "").strip()
    if not name:
        return "no BLOCK_FUNCTION is configured"
    return function_unreachable_reason(name) or ""


def enforcement_mode() -> str:
    """The configured mode, normalized. An unknown value reads as ``auto``."""
    from .conf import chat_settings

    mode = str(chat_settings.BLOCK_ENFORCEMENT or ENFORCEMENT_AUTO).strip().lower()
    return mode if mode in ENFORCEMENT_MODES else ENFORCEMENT_AUTO


def blocked_pairs(pairs) -> set:
    """Ask the provider which of ``pairs`` are blocked, in either direction.

    Returns a set of ``frozenset({a, b})``, so a caller never has to remember
    which way round it asked — which is also how direction stays unreportable
    by construction rather than by discipline.

    Raises :class:`BlockCheckUnavailable` when the provider is configured and
    then fails, and when the deployment declared enforcement ``required`` and
    the provider is not there. Those are the two cases that must not read as
    "allowed".
    """
    from stapel_core.comm import call

    from .conf import chat_settings

    mode = enforcement_mode()
    if mode == ENFORCEMENT_OFF:
        return set()

    wanted = [
        [str(a), str(b)] for a, b in pairs if str(a) and str(b) and str(a) != str(b)
    ]
    if not wanted:
        return set()

    unreachable = provider_unreachable_reason()
    if unreachable:
        if mode == ENFORCEMENT_REQUIRED:
            # Declared required and not there: the deployment is broken and
            # says so, rather than letting a blocked sender through.
            raise BlockCheckUnavailable(unreachable)
        # "auto": no block store in this deployment. Announced at every boot
        # by checks.W003 — this is never the first time anybody hears it.
        return set()

    name = (chat_settings.BLOCK_FUNCTION or "").strip()
    try:
        answer = call(
            name,
            {"pairs": wanted},
            timeout=float(chat_settings.BLOCK_TIMEOUT_S),
        )
    except Exception as exc:  # noqa: BLE001 — an outage is not consent
        logger.warning("stapel_chat: block check via %r failed: %s", name, exc)
        raise BlockCheckUnavailable(str(exc)) from exc

    blocked = (answer or {}).get("blocked") or []
    return {frozenset((str(a), str(b))) for a, b in blocked if a and b}


def is_blocked(user_a, user_b) -> bool:
    """Whether a block exists between two users, in either direction."""
    return frozenset((str(user_a), str(user_b))) in blocked_pairs([(user_a, user_b)])


__all__ = [
    "BlockCheckUnavailable",
    "ENFORCEMENT_AUTO",
    "ENFORCEMENT_MODES",
    "ENFORCEMENT_OFF",
    "ENFORCEMENT_REQUIRED",
    "blocked_pairs",
    "enforcement_mode",
    "is_blocked",
    "provider_unreachable_reason",
]
