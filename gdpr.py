"""GDPR data handler for stapel-chat.

This module holds user PII: ``Message.sender`` + ``Message.body`` (authored
content), ``ConversationParticipant.user`` (membership) and
``Conversation.assigned_operator``. Per the Stapel standard, a data-holding
module subscribes to ``user.deleted`` and erases/anonymizes that data.

- The user's authored messages become **anonymous tombstones**: body gone,
  attachments gone, sender detached, ``deleted_at`` stamped, and a fresh
  ``rev_seq`` so the erasure travels to every client cache on the next replay.
  Hard-deleting the rows — what this provider did before 0.3.0 — destroyed the
  content on the server and left every copy on every other participant's
  device, because nothing told those devices which ids had ceased to exist. It
  also tore gaps in a sequence the realtime protocol assumes is gapless.
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
        from . import services
        from .models import Conversation, ConversationKind, ConversationParticipant

        # Conversations the user touched — candidates for direct-thread cleanup.
        conv_ids = set(
            ConversationParticipant.objects.filter(user_id=user_id).values_list(
                "conversation_id", flat=True
            )
        )
        # The user's authored content is destroyed — and the destruction is
        # published, which a DELETE could never be. See the module docstring.
        services.erase_user_messages(user_id)
        # Their membership is their PII. Kick any socket they still hold open
        # before the row that authorizes it disappears underneath them.
        for conv_id in conv_ids:
            services.realtime.revoke_participant(
                conv_id, user_id, reason="account_deleted"
            )
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
