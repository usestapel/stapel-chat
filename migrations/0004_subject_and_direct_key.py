"""Subjects on a conversation, and a direct thread keyed by one.

Expand-only (release-management.md §3): two new blank-defaulted columns, one
widened CharField, one new index. No column is dropped, no value rewritten,
and nothing here needs a backfill — the subject-less ``direct_key`` produced
by the new code is byte-identical to the one produced by the old, so every
conversation that already exists keeps its identity and keeps being found.

What CHANGES is what a *new* create can produce: a pair who ask for a thread
about a subject now get a thread of their own instead of being folded into
the single thread they were previously condemned to share.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0003_backfill_rev_seq_and_attachments"),
    ]

    operations = [
        migrations.AlterField(
            model_name="conversation",
            name="direct_key",
            field=models.CharField(blank=True, default="", max_length=900),
        ),
        migrations.AddField(
            model_name="conversation",
            name="subject_type",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="conversation",
            name="subject_key",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddIndex(
            model_name="conversation",
            index=models.Index(
                fields=["subject_type", "subject_key"], name="chat_conv_subject"
            ),
        ),
    ]
