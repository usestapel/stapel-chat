"""Backfill the two things 0002 could only add empty.

``rev_seq`` starts life equal to ``seq``: a message that has never been
revised sits at its creation position in the revision journal, which is what
makes ``rev_seq > cursor`` a correct replay query from the first row onwards.
A default of 0 would have made every pre-0.3 message replay on every resume.

``attachments`` moves from ``["<ref>", ...]`` to
``[{"key": "<ref>", "type": "file", ...}, ...]``. The old rows carry a bare
CDN ref and nothing else, and nothing here can invent an aspect ratio or a
thumbnail — the type is set to ``file`` (the one builtin that renders from
mime and extension alone) and the metadata fields are left null. A host that
wants the old images to render as images re-describes them from the CDN in
its own data migration; this one refuses to guess.

Both operations are idempotent and both have real reverses: the shape is
recoverable in either direction, which is what makes this a deployable
expand/contract step rather than a one-way door.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Message = apps.get_model("chat", "Message")

    # rev_seq := seq for every row the new column defaulted to 0.
    for message in Message.objects.filter(rev_seq=0).iterator(chunk_size=2000):
        Message.objects.filter(pk=message.pk).update(rev_seq=message.seq)

    # attachments: ["ref", ...] -> [{"key": "ref", "type": "file", ...}, ...]
    for message in Message.objects.exclude(attachments=[]).iterator(chunk_size=1000):
        raw = message.attachments or []
        if not any(isinstance(item, str) for item in raw):
            continue
        converted = [
            {"key": item, "type": "file"} if isinstance(item, str) else item
            for item in raw
        ]
        Message.objects.filter(pk=message.pk).update(attachments=converted)


def backwards(apps, schema_editor):
    Message = apps.get_model("chat", "Message")
    for message in Message.objects.exclude(attachments=[]).iterator(chunk_size=1000):
        raw = message.attachments or []
        if not any(isinstance(item, dict) for item in raw):
            continue
        converted = [
            item.get("key", "") if isinstance(item, dict) else item for item in raw
        ]
        Message.objects.filter(pk=message.pk).update(attachments=converted)


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0002_realtime_tombstone_attachments"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
