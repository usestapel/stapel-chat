"""stapel-chat capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_chat._codegen import _configure

    _configure()
    from stapel_chat.conf import DEFAULTS
    from stapel_chat.urls import GATE_REGISTRY

    # CTO-facing config axes (capability-config.md §16), all behavioral (they
    # widen/narrow what endpoints accept, they do not unmount any URL):
    #   CHAT_KINDS            (list) — which conversation kinds are offered
    #   ATTACHMENTS           (bool) — whether messages may carry attachments
    #   MAX_BODY_LENGTH       (int)  — hard cap on a text body
    #   ATTACHMENT_TYPES      (open registry) — image/gif/video/voice/file +
    #                                 whatever the host adds (stickers)
    #   ACTIVITY_STATES       (open registry) — typing/recording/uploading + …
    #   ATTACHMENT_METADATA   (enum) — cdn.describe, or trust the client
    #   MAX_ATTACHMENTS / MAX_PREVIEW_B64_BYTES / EDIT_WINDOW_S (int limits)
    #   MODERATION_TARGET_TYPE (enum) — whether a message is reportable, and
    #                                 under which stapel-moderation target
    #
    # There is deliberately NO realtime axis: the socket is the path, and a
    # deployment that cannot serve one fails manage.py check rather than
    # degrading into a polling product. SCOPE_PROVIDER is the one extension
    # seam (curated in docs/capabilities.meta.json), not an axis.
    axes = {
        "CHAT_KINDS",
        "ATTACHMENTS",
        "MAX_BODY_LENGTH",
        "ATTACHMENT_TYPES",
        "ACTIVITY_STATES",
        "ATTACHMENT_METADATA",
        "MAX_ATTACHMENTS",
        "MAX_PREVIEW_B64_BYTES",
        "EDIT_WINDOW_S",
        "MODERATION_TARGET_TYPE",
    }
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
                "ATTACHMENT_TYPES": "chat.attachments",
                "ATTACHMENT_METADATA": "chat.attachments",
                "MAX_ATTACHMENTS": "chat.attachments",
                "ACTIVITY_STATES": "chat.realtime",
                "MAX_BODY_LENGTH": "chat.limits",
                "MAX_PREVIEW_B64_BYTES": "chat.limits",
                "EDIT_WINDOW_S": "chat.limits",
                "MODERATION_TARGET_TYPE": "chat.moderation",
            }
        ),
        prog="stapel-chat-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
