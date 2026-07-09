from django.apps import AppConfig


class ChatConfig(AppConfig):
    name = "stapel_chat"
    label = "chat"
    verbose_name = "Chat and messaging"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects: comm actions/functions, system checks,
        # error-key registration. Keep each in its own module. Realtime
        # (consumers.py) is deliberately NOT imported here — Channels is an
        # optional extra and an HTTP-only host must not pay for it.
        from . import actions  # noqa: F401
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401

        # GDPR: register the per-app data handler (monolith in-process mode).
        from stapel_core.gdpr import gdpr_registry

        from .gdpr import ChatGDPRProvider

        if not any(p.section == "chat" for p in gdpr_registry.providers):
            gdpr_registry.register(ChatGDPRProvider())
