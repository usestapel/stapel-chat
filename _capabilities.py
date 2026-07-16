"""stapel-chat capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_chat._codegen import _configure

    _configure()
    from stapel_chat.conf import DEFAULTS
    from stapel_chat.urls import GATE_REGISTRY

    # Three CTO-facing config axes (capability-config.md §16), all behavioral
    # (they widen/narrow what endpoints accept, they do not unmount any URL):
    #   CHAT_KINDS    (list) — which conversation kinds are offered
    #   ATTACHMENTS   (bool) — whether messages may carry attachment keys
    #   MAX_BODY_LENGTH (int→enum kind) — hard cap on a text body
    # SCOPE_PROVIDER is the one extension seam (curated in
    # docs/capabilities.meta.json), not an axis.
    axes = {"CHAT_KINDS", "ATTACHMENTS", "MAX_BODY_LENGTH"}
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/chat/api/v1",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: k in axes,
        axis_group=axis_group_rules(
            exact={
                "CHAT_KINDS": "chat.kinds",
                "ATTACHMENTS": "chat.attachments",
                "MAX_BODY_LENGTH": "chat.limits",
            }
        ),
        prog="stapel-chat-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
