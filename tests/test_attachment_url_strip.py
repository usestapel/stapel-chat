"""``chat_strip_attachment_urls``: stored URLs removed in bounded batches."""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from stapel_chat import services
from stapel_chat.attachment_url_strip import strip_attachment_urls
from stapel_chat.models import Message

pytestmark = pytest.mark.django_db

VOICE = "audio/" + "a1" * 32
LEGACY = {
    "key": VOICE, "type": "audio", "mime": "audio/ogg", "duration_ms": 4200,
    "meta_status": "ok", "meta_reason": None, "poster_url": None,
    "variants": [{"tier": "original", "url": "/media/private/audio/a.ogg"}],
}


def _legacy_rows(user, other_user, n):
    conv = services.create_direct(owner=user, other_user_id=other_user.id)
    ids = []
    for _ in range(n):
        msg = services.post_message(conversation=conv, sender=user, body="x")
        Message.objects.filter(pk=msg.pk).update(attachments=[dict(LEGACY)])
        ids.append(msg.pk)
    return conv, ids


def test_old_rows_lose_their_urls_and_keep_their_facts(user, other_user):
    _, ids = _legacy_rows(user, other_user, 5)
    rev_before = dict(Message.objects.values_list("pk", "rev_seq"))
    stats = strip_attachment_urls(batch_size=2)
    assert stats == {"scanned": 5, "rewritten": 5}
    for msg in Message.objects.filter(pk__in=ids):
        [stored] = msg.attachments
        assert "variants" not in stored and "poster_url" not in stored
        assert stored["duration_ms"] == 4200 and stored["key"] == VOICE
    assert dict(Message.objects.values_list("pk", "rev_seq")) == rev_before


def test_a_rerun_and_a_dry_run_write_nothing(user, other_user):
    _legacy_rows(user, other_user, 3)
    assert strip_attachment_urls(dry_run=True)["rewritten"] == 3
    assert all(m.attachments[0].get("variants") for m in Message.objects.all())
    strip_attachment_urls()
    assert strip_attachment_urls()["rewritten"] == 0


def test_limit_slices_the_pass(user, other_user):
    _legacy_rows(user, other_user, 4)
    assert strip_attachment_urls(batch_size=10, limit=3)["rewritten"] == 3
    assert strip_attachment_urls()["rewritten"] == 1


def test_a_tombstone_is_left_alone(user, other_user):
    conv, [msg_id] = _legacy_rows(user, other_user, 1)
    msg = Message.objects.get(pk=msg_id)
    services.delete_message(message=msg, actor=user)
    strip_attachment_urls()
    assert Message.objects.get(pk=msg_id).attachments == []


def test_the_command_reports(user, other_user):
    _legacy_rows(user, other_user, 2)
    out = StringIO()
    call_command("chat_strip_attachment_urls", "--batch-size", "1", stdout=out)
    assert "rewrote 2" in out.getvalue()
