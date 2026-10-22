"""Membership removal revokes the open subscription, whatever removed it.

Leave and GDPR erasure revoked; the admin inline, a guest row folded away by
``user.merged`` and a conversation deleted with its participants did not, so
their sockets kept receiving live frames until reconnect. A ``post_delete``
receiver is the one place every removal path passes through, including the
cascade of a deleted conversation or user.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import ConversationParticipant


@receiver(post_delete, sender=ConversationParticipant, dispatch_uid="chat_participant_revoke")
def revoke_on_participant_delete(sender, instance, **kwargs):
    from . import realtime

    conversation_id, user_id = instance.conversation_id, instance.user_id
    transaction.on_commit(
        lambda: realtime.revoke_participant(
            conversation_id, user_id, reason="removed_from_conversation"
        )
    )
