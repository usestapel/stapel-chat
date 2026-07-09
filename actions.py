"""Action subscriptions of stapel-chat.

Handlers must be idempotent: delivery is at-least-once (outbox retries, broker
redelivery). Consumes contracts live in ``schemas/consumes/``.
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase this module's PII when an account deletion is executed: the user's
    authored messages, their conversation participations, and any direct
    conversation that becomes empty as a result."""
    from .gdpr import ChatGDPRProvider

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    ChatGDPRProvider().delete(user_id)
    logger.info("chat data erased for deleted user %s", user_id)
