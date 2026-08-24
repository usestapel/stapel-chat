from django.apps import AppConfig


class ChatConfig(AppConfig):
    name = "stapel_chat"
    label = "chat"
    verbose_name = "Chat and messaging"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects: comm actions/functions, system checks,
        # error-key registration. Keep each in its own module.
        #
        # consumers.py is still NOT imported here — the routes are discovered
        # from routing.py by stapel_realtime's host assembly, and a build that
        # only needs the models or the emit schemas should not be made to
        # import an ASGI stack. That is a packaging boundary, not an "optional
        # realtime": checks.py refuses to boot a deployment that cannot serve
        # the socket.
        from . import actions  # noqa: F401
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401

        # Moderation: declare what a "chat message" is to a target-generic
        # queue — but only into a gap. A host that declared the type itself
        # (the stapel-classified preset does) keeps its own policy, because
        # the runtime registry layer would otherwise overwrite settings.
        from .moderation import register_moderation_target

        register_moderation_target()

        # GDPR: register the per-app data handler (monolith in-process mode).
        from stapel_core.gdpr import gdpr_registry

        from .gdpr import ChatGDPRProvider

        if not any(p.section == "chat" for p in gdpr_registry.providers):
            gdpr_registry.register(ChatGDPRProvider())
