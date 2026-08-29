"""Presence: whether a user is connected, and when they last were.

One new table, keyed by the user. Nothing existing is touched — no column
added to Conversation or Participant, no value rewritten — so the whole
migration is additive and reversible by dropping the table.

There is deliberately no backfill. Presence is a fact about live sockets, and
a user nobody has seen connect since this table existed is offline with no
last-seen, which the read path reports as exactly that. Inventing a
``last_seen_at`` of "now" at migration time would tell every peer that
everybody was just here, which is a worse lie than the "На связи" this
release exists to delete.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0004_subject_and_direct_key'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserPresence',
            fields=[
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='chat_presence', serialize=False, to=settings.AUTH_USER_MODEL)),
                ('connections', models.IntegerField(default=0)),
                ('online_until', models.DateTimeField(blank=True, null=True)),
                ('last_seen_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'indexes': [models.Index(fields=['online_until'], name='chat_presence_lease')],
            },
        ),
    ]
