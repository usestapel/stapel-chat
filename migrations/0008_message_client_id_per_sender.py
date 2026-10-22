"""Send idempotency per sender, not per conversation.

The new constraints are each LOOSER than the one they replace (it covered
every non-blank key in a conversation; they split it by sender, and system
lines by conversation), so every row that exists already satisfies them and
nothing is rewritten or deleted. They are added before the old one is
dropped, so there is no moment without a uniqueness guarantee.
"""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0007_participant_cleared_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='message',
            constraint=models.UniqueConstraint(condition=models.Q(models.Q(('client_msg_id', ''), _negated=True), models.Q(('kind', 'system'), _negated=True)), fields=('conversation', 'sender', 'client_msg_id'), name='chat_message_uniq_sender_client_id'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.UniqueConstraint(condition=models.Q(models.Q(('client_msg_id', ''), _negated=True), ('kind', 'system')), fields=('conversation', 'client_msg_id'), name='chat_message_uniq_system_client_id'),
        ),
        migrations.RemoveConstraint(
            model_name='message',
            name='chat_message_uniq_client_id',
        ),
    ]
