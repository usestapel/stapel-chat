"""The moderation seam of stapel-chat — one target type, registered openly.

stapel-moderation is target-generic: it ships an EMPTY target registry and
learns what a "chat message" is from whoever knows. Until now nobody did, so
the only way to file a complaint about a message in the fleet was
stapel-classified's *evidence-based* ``chat_message`` policy — the reporter's
own screenshot, marked unverified, because no module served the message. This
module stores every message it delivers, so that workaround was never the
truth: chat can answer for its own content, and now does
(``chat.moderation_content``, ``services.moderation_content``).

**Registration is optional in both directions.** stapel-moderation is not a
dependency of this package: without it installed nothing here runs and nothing
breaks. And a host that has already declared ``chat_message`` — the
stapel-classified preset does — keeps ITS policy: the runtime registry layer
wins over settings, so registering unconditionally would silently overwrite a
composite's deliberate reasons list with this module's defaults. The rule is
therefore "fill the gap, never overrule the host", and ``MODERATION_TARGET_TYPE
= ""`` turns even that off.

There is no *conversation* target. A verdict's consequence is a sanction on an
author, and a thread has none; a complaint about a thread is a complaint about
the messages in it, and each one carries its ``conversation_id`` in the card's
extra so a moderator can see the context they came from.

There is no verdict CONSUMER either, and ``verdict_event`` is an explicit
``None`` rather than an omission — stapel-moderation announces that fact as
``W006`` instead of leaving it to be discovered. Chat does not take a message
down on a verdict: deletion here is an author's act and a tombstone that
travels to every client, and wiring a moderator's verdict into it is a
product decision with a notification and an appeal attached, not a line in a
registration helper.
"""
from __future__ import annotations

#: The name the fleet already uses (stapel-classified's preset declares
#: reasons with ``applies_to: ["seller", "chat_message"]``). A second spelling
#: for the same thing would split the queue in two.
MESSAGE_TARGET_TYPE = "chat_message"

#: The comm Function serving a message's live content (``functions.py``).
CONTENT_FUNCTION = "chat.moderation_content"

#: The policy this module registers for :data:`MESSAGE_TARGET_TYPE`.
#:
#: A host overrides any of it by declaring ``chat_message`` itself — see the
#: module docstring for why that declaration wins.
MESSAGE_TARGET_POLICY: dict = {
    # A message is live the moment it is sent; there is no pre-publication
    # gate to hold it behind, and building one would be a different product.
    "gate": "post",
    # No intake topic: a case per message would screen every conversation in
    # the deployment. Cases here are opened by REPORTS.
    "intake_events": [],
    # The *.moderation_content family takes the owner's own id name. The
    # VALUE may be a bare message id or stapel-classified's composite
    # <conversation_id>:<message_id> — see services.MESSAGE_KEY_SEPARATOR.
    "id_field": "message_id",
    "content_function": CONTENT_FUNCTION,
    # Nothing consumes a verdict about a message (see the module docstring).
    # Explicit None is a statement; moderation announces it as W006.
    "verdict_event": None,
    # No `chat_message_blocked` notification type exists in the fleet, and
    # naming one that is not registered is moderation.E005.
    "notification_types": {},
    # Attachment keys are opaque CDN handles, not images a screener can read.
    "media": False,
    # The universal taxonomy minus the codes that are about GOODS:
    # `counterfeit`, `wrong_category` and `misleading_price` describe an
    # offer, and the place a verdict can remove an offer is the listing.
    "reasons": [
        "spam",
        "offensive",
        "harassment",
        "fraud",
        "illegal",
        "adult",
        "personal_data",
        "off_platform_payment",
        "other",
    ],
}


def moderation_installed() -> bool:
    """Is stapel-moderation importable in this deployment?"""
    from importlib.util import find_spec

    try:
        return find_spec("stapel_moderation") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def register_moderation_target() -> bool:
    """Register :data:`MESSAGE_TARGET_TYPE` unless somebody already did.

    Returns True when this call is what put the type in the registry. Called
    from ``apps.ready()``; safe to call again (the second call sees the type
    declared and steps back).
    """
    from .conf import chat_settings

    name = chat_settings.MODERATION_TARGET_TYPE
    if not name or not moderation_installed():
        return False

    from stapel_moderation.registry import get_target_types, register_target_type

    if name in get_target_types():
        # The host declared it — in settings (a preset) or at runtime. Its
        # policy is a decision about this deployment; ours is a default.
        return False
    register_target_type(name, dict(MESSAGE_TARGET_POLICY))
    return True


__all__ = [
    "CONTENT_FUNCTION",
    "MESSAGE_TARGET_POLICY",
    "MESSAGE_TARGET_TYPE",
    "moderation_installed",
    "register_moderation_target",
]
