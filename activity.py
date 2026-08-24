"""The activity-state registry — "typing…", and everything after it.

An activity state is what one participant is *doing right now*: typing,
recording audio, uploading a file. It is the purest Signal in the system —
worthless a second later, recoverable from nothing, owed to nobody who was
not watching. So it never touches the database and never rides the outbox.

Like the attachment registry this is **open**, merge-over-builtins: builtins
<- ``STAPEL_CHAT["ACTIVITY_STATES"]`` <- :func:`register_activity_state`,
later wins, ``None`` removes. The owner has already named the next one
("choosing a sticker"); a closed enum would make it a release here instead of
a settings line in the host.

A state is a plain string with a declared TTL — how long a client should keep
showing it without a refresh. There is no "stop typing" obligation on the
client: a state expires on its own, which is the only design that survives a
browser tab being closed mid-word.
"""
from __future__ import annotations

from typing import Optional

#: Default seconds a state stays on screen without a refresh frame.
DEFAULT_TTL_S = 6

#: Builtin states. ``ttl_s`` is the client's expiry hint; ``label`` is a key
#: for the host's i18n catalog, never a rendered string.
BUILTIN_ACTIVITY_STATES: dict[str, Optional[dict]] = {
    # The absence of activity. Sent to retract a state early (the client
    # cleared the compose box); never required, because every state expires.
    "idle": {"ttl_s": 0, "label": "chat.activity.idle"},
    "typing": {"ttl_s": DEFAULT_TTL_S, "label": "chat.activity.typing"},
    "recording_audio": {"ttl_s": 30, "label": "chat.activity.recording_audio"},
    "sending_video": {"ttl_s": 60, "label": "chat.activity.sending_video"},
    "uploading_file": {"ttl_s": 60, "label": "chat.activity.uploading_file"},
}

_runtime_states: dict[str, Optional[dict]] = {}


class UnknownActivityState(Exception):
    """An activity frame names a state no layer of the registry provides."""


def register_activity_state(name: str, spec: Optional[dict]) -> None:
    """Register/override an activity state at runtime. ``None`` removes one."""
    _runtime_states[name] = spec


def reset_activity_states() -> None:
    """Tests only: drop runtime activity-state overrides."""
    _runtime_states.clear()


def get_activity_states() -> dict[str, dict]:
    """Effective registry: builtins <- settings <- runtime, ``None`` removes."""
    from .conf import chat_settings

    merged: dict[str, Optional[dict]] = dict(BUILTIN_ACTIVITY_STATES)
    for source in (chat_settings.ACTIVITY_STATES or {}, _runtime_states):
        for name, spec in source.items():
            merged[name] = spec
    return {name: spec for name, spec in merged.items() if spec is not None}


def activity_state_names() -> tuple[str, ...]:
    """Sorted names of every live activity state."""
    return tuple(sorted(get_activity_states()))


def resolve_activity(state: str) -> dict:
    """Validate a state name and return ``{"state", "ttl_s"}``.

    Raises :class:`UnknownActivityState` — an unrecognized state is refused
    rather than forwarded, because the whole point of the registry is that
    every subscriber knows what it may be asked to render.
    """
    states = get_activity_states()
    if state not in states:
        raise UnknownActivityState(state)
    spec = states[state] or {}
    return {"state": state, "ttl_s": int(spec.get("ttl_s", DEFAULT_TTL_S))}


__all__ = [
    "BUILTIN_ACTIVITY_STATES",
    "DEFAULT_TTL_S",
    "UnknownActivityState",
    "activity_state_names",
    "get_activity_states",
    "register_activity_state",
    "reset_activity_states",
    "resolve_activity",
]
