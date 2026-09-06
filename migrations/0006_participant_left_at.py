"""Leaving a conversation: one nullable column on the participant row.

Purely additive and reversible by dropping the column. Nothing is backfilled
and nothing existing is rewritten: ``null`` means "still in the thread", which
is what every participant that exists when this migration runs is.

No index. Every read of ``left_at`` is already narrowed to one user's
participations by ``chat_participant_user``, and an index on a column that is
null for all but a handful of rows would be a write cost on every send in
exchange for nothing.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0005_user_presence'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversationparticipant',
            name='left_at',
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
    ]
