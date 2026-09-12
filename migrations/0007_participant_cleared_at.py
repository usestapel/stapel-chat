"""Clear history for me: one nullable column on the participant row.

Purely additive and reversible by dropping the column. Nothing is backfilled
and nothing existing is rewritten: ``null`` means "this person has never
cleared this thread", which is what every participant that exists when this
migration runs is. No message row is read or written by this migration, and
none ever is by the feature — clearing is a mark on the reader, not a state on
the message.

No index, for the reason ``left_at`` (0006) has none: every read of it is
already narrowed to one user's participations by ``chat_participant_user``,
and a second index on a column that is null for almost every row would be paid
for by every send.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0006_participant_left_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversationparticipant',
            name='cleared_at',
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
    ]
