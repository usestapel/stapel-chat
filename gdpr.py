"""GDPR data handler for stapel-chat.

This module holds user PII: ``Message.sender`` + ``Message.body`` (authored
content), ``ConversationParticipant.user`` (membership) and
``Conversation.assigned_operator``. Per the Stapel standard, a data-holding
module subscribes to ``user.deleted`` and erases/anonymizes that data.

- The user's authored messages are hard-deleted (their body is their content).
- The user's participations are removed (membership is their PII).
- A ``direct`` conversation that is left with fewer than two participants after
  the erasure carries no further purpose and is deleted (cascading its
  remaining messages). Group/support threads are retained for the other
  members; the departed user simply no longer appears.
"""
from stapel_core.gdpr import GDPRProvider


class ChatGDPRProvider(GDPRProvider):
    section = "chat"

    def export(self, user_id) -> dict:
        from .models import ConversationParticipant, Message

        messages = list(
            Message.objects.filter(sender_id=user_id).values(
                "id", "conversation_id", "seq", "kind", "body", "created_at"
            )
        )
        participations = list(
            ConversationParticipant.objects.filter(user_id=user_id).values(
                "conversation_id", "role", "last_read_seq"
            )
        )
        return {
            "messages": _serialize(messages),
            "participations": _serialize(participations),
        }

    def delete(self, user_id) -> None:
        from .models import (
            Conversation,
            ConversationKind,
            ConversationParticipant,
            Message,
        )

        # Conversations the user touched — candidates for direct-thread cleanup.
        conv_ids = set(
            ConversationParticipant.objects.filter(user_id=user_id).values_list(
                "conversation_id", flat=True
            )
        )
        # The user's authored messages are their content.
        Message.objects.filter(sender_id=user_id).delete()
        # Their membership is their PII.
        ConversationParticipant.objects.filter(user_id=user_id).delete()

        # A direct thread with fewer than two remaining participants is dead.
        for conv in Conversation.objects.filter(
            id__in=conv_ids, kind=ConversationKind.DIRECT
        ):
            if conv.participants.count() < 2:
                conv.delete()

    def anonymize(self, user_id) -> None:
        # Chat content is deleted, not retained-and-anonymized.
        pass


def _serialize(rows: list) -> list:
    return [
        {k: v.isoformat() if hasattr(v, "isoformat") else str(v) for k, v in row.items()}
        for row in rows
    ]
