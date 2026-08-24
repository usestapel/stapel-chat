"""What a conversation is ABOUT — an opaque pair, and who can render it.

The product finding that routed this here: a chat opened in a live classified
marketplace was *"unclear with whom, and unclear about what"*. A messaging
engine cannot answer the second half by itself — it does not know what a
listing is, and it must not learn — so it carries two opaque strings and asks
somebody who does.

``(subject_type, subject_key)`` is moderation's ``(target_type, target_key)``
idiom, for the same reason: the pair is a NAME, never a parsed thing. This
module stores it, hashes it into the direct thread's identity, and resolves it
to a *card* by calling a comm Function the registry names. Nothing here ever
looks inside a card.

**The registry ships EMPTY.** A generic chat has no subject types, and the
built-in that would have gone here — ``listing`` — belongs to whoever owns
listings, not to messaging. A host declares its types in
``STAPEL_CHAT["SUBJECT_TYPES"]`` or at runtime; merge over builtins, later
wins, ``None`` removes, exactly like the attachment and activity registries.

**The card function is batched and answers for every key it was asked about.**
``{keys: [...]} -> {cards: {key: card}}``. The shape is stapel-classified's
``classified.subject_cards``, which was designed against this ask before this
existed; a key whose subject was deleted comes back as a ``gone`` card rather
than being dropped, because the caller is rendering a conversation that still
exists. Chat holds the provider to the same rule: a key the provider omits is
reported as a degraded card, never as a silently absent one.

**Degradation is data.** Every resolution carries ``meta_status`` /
``meta_reason``, so a header that could not be built says WHY —
``subject_type_unregistered``, ``card_function_unreachable``,
``card_function_failed``, ``card_missing``. A conversation never fails to open
because a catalogue blinked.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

#: Ships empty, on purpose — see the module docstring.
BUILTIN_SUBJECT_TYPES: dict[str, Optional[dict]] = {}

#: What a policy may declare. ``card_function`` is the only required key.
POLICY_DEFAULTS: dict[str, Any] = {
    # The comm Function that turns keys into cards. Required; a policy
    # without one is refused by register_subject_type and announced by
    # stapel_chat.E020.
    "card_function": "",
    # The batch request/response field names, defaulted to the shape
    # classified.subject_cards already serves. They exist so a deployment can
    # point at a provider that spells them differently without forking.
    "request_field": "keys",
    "response_field": "cards",
    # i18n key for a rendered label ("Listing"), never a rendered string.
    "label": "",
}

META_OK = "ok"
META_PARTIAL = "partial"
META_MISSING = "missing"

#: Why a subject has no card. Named rather than left to a null.
REASON_UNREGISTERED = "subject_type_unregistered"
REASON_UNREACHABLE = "card_function_unreachable"
REASON_FAILED = "card_function_failed"
REASON_CARD_MISSING = "card_missing"

_runtime_types: dict[str, Optional[dict]] = {}


class UnknownSubjectType(Exception):
    """A conversation names a subject type no layer of the registry provides."""


class InvalidSubjectPolicy(Exception):
    """A subject-type policy names no ``card_function``."""


def register_subject_type(name: str, spec: Optional[dict]) -> None:
    """Register/override a subject type at runtime. ``None`` removes one."""
    if spec is not None and not (spec.get("card_function") or "").strip():
        raise InvalidSubjectPolicy(
            f"subject type {name!r} declares no card_function — a subject "
            "nobody can render is a string in a database, not a header"
        )
    _runtime_types[name] = spec


def reset_subject_types() -> None:
    """Tests only: drop runtime subject-type overrides."""
    _runtime_types.clear()


def get_subject_types() -> dict[str, dict]:
    """Effective registry: builtins <- settings <- runtime, ``None`` removes.

    Every returned policy is complete — missing keys filled from
    :data:`POLICY_DEFAULTS` — so no caller has to remember a default.
    """
    from .conf import chat_settings

    merged: dict[str, Optional[dict]] = dict(BUILTIN_SUBJECT_TYPES)
    for source in (chat_settings.SUBJECT_TYPES or {}, _runtime_types):
        for name, spec in source.items():
            merged[name] = spec
    return {
        name: {**POLICY_DEFAULTS, **(spec or {})}
        for name, spec in merged.items()
        if spec is not None
    }


def subject_type_names() -> tuple[str, ...]:
    """Sorted names of every registered subject type."""
    return tuple(sorted(get_subject_types()))


def resolve_subject_type(name: str) -> dict:
    """The policy for ``name``, or raise :class:`UnknownSubjectType`.

    An unregistered type is refused at the door rather than stored: a
    conversation whose subject nothing can render is the "unclear about what"
    this whole surface exists to close.
    """
    types = get_subject_types()
    if name not in types:
        raise UnknownSubjectType(name)
    return types[name]


# ── Card resolution ──────────────────────────────────────────────────────


def _empty(reason: str, status: str = META_PARTIAL) -> dict:
    return {"card": None, "meta_status": status, "meta_reason": reason}


def resolve_cards(pairs: Iterable[tuple]) -> dict[tuple, dict]:
    """``[(subject_type, subject_key), …] -> {(type, key): resolution}``.

    **One call per subject type, not one per conversation.** A page of fifty
    conversations about fifty listings costs one round trip, which is the
    whole reason the provider contract is a batch.

    Each resolution is ``{"card", "meta_status", "meta_reason"}``. The card is
    whatever the provider answered, passed through untouched — this module
    does not know what a listing is and never will.
    """
    from stapel_core.comm import call, function_unreachable_reason

    from .conf import chat_settings

    wanted: dict[str, set] = {}
    for subject_type, subject_key in pairs:
        if not subject_type or not subject_key:
            continue
        wanted.setdefault(str(subject_type), set()).add(str(subject_key))
    if not wanted:
        return {}

    types = get_subject_types()
    timeout = float(chat_settings.SUBJECT_CARD_TIMEOUT_S)
    out: dict[tuple, dict] = {}

    for subject_type, keys in wanted.items():
        policy = types.get(subject_type)
        if policy is None:
            # The type is on rows in the database and in no registry — a
            # deployment that removed it, or a row that predates it.
            for key in keys:
                out[(subject_type, key)] = _empty(REASON_UNREGISTERED, META_MISSING)
            continue

        name = policy["card_function"]
        unreachable = function_unreachable_reason(name) if name else "not declared"
        if unreachable:
            logger.warning(
                "stapel_chat: subject card function %r is unreachable: %s",
                name,
                unreachable,
            )
            for key in keys:
                out[(subject_type, key)] = _empty(REASON_UNREACHABLE)
            continue

        try:
            answer = call(name, {policy["request_field"]: sorted(keys)}, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — a header is never worth a 500
            logger.warning(
                "stapel_chat: subject card function %r failed: %s", name, exc
            )
            for key in keys:
                out[(subject_type, key)] = _empty(REASON_FAILED)
            continue

        cards = (answer or {}).get(policy["response_field"]) or {}
        if not isinstance(cards, dict):
            cards = {}
        for key in keys:
            card = cards.get(key)
            if card is None:
                # The provider's own contract says a key it cannot serve
                # comes back as a `gone` card rather than being omitted. An
                # omission is therefore a provider defect, and it is reported
                # as one instead of rendering as "no subject".
                out[(subject_type, key)] = _empty(REASON_CARD_MISSING)
            else:
                out[(subject_type, key)] = {
                    "card": card,
                    "meta_status": META_OK,
                    "meta_reason": None,
                }
    return out


__all__ = [
    "BUILTIN_SUBJECT_TYPES",
    "InvalidSubjectPolicy",
    "META_MISSING",
    "META_OK",
    "META_PARTIAL",
    "POLICY_DEFAULTS",
    "REASON_CARD_MISSING",
    "REASON_FAILED",
    "REASON_UNREACHABLE",
    "REASON_UNREGISTERED",
    "UnknownSubjectType",
    "get_subject_types",
    "register_subject_type",
    "reset_subject_types",
    "resolve_cards",
    "resolve_subject_type",
    "subject_type_names",
]
