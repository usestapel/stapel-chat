"""seq monotonicity / concurrency backstop + outbox atomicity (emit-check)."""
import subprocess
import sys
from pathlib import Path

import pytest
from django.db import IntegrityError

from stapel_chat import services
from stapel_chat.models import Message

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.django_db
class TestSeqConcurrency:
    def test_sequential_sends_are_strictly_monotonic(self, user):
        conv = services.create_group(owner=user)
        seqs = [
            services.post_message(conversation=conv, sender=user, body=str(i)).seq
            for i in range(20)
        ]
        assert seqs == list(range(1, 21))

    def test_unique_constraint_is_the_backstop(self, user):
        """The (conversation, seq) unique constraint rejects a duplicate seq —
        the guarantee the retry loop leans on when two senders race."""
        conv = services.create_group(owner=user)
        Message.objects.create(conversation=conv, sender=user, seq=1, body="a")
        with pytest.raises(IntegrityError):
            Message.objects.create(conversation=conv, sender=user, seq=1, body="b")

    def test_post_retries_past_a_seq_collision(self, user, monkeypatch):
        """A seq collision (the concurrent-sender case) is caught and retried —
        the second attempt allocates the next seq rather than failing."""
        conv = services.create_group(owner=user)
        real_post_once = services._post_once
        state = {"n": 0}

        def flaky(*args, **kwargs):
            state["n"] += 1
            if state["n"] == 1:
                raise IntegrityError("simulated seq race")
            return real_post_once(*args, **kwargs)

        monkeypatch.setattr(services, "_post_once", flaky)
        msg = services.post_message(conversation=conv, sender=user, body="hi")
        assert state["n"] == 2
        assert msg.seq == 1  # the winner's advance was rolled back; retry re-allocates


@pytest.mark.django_db
class TestOutboxAtomicity:
    def test_message_and_event_commit_together(self, user, captured_events):
        conv = services.create_group(owner=user)
        msg = services.post_message(conversation=conv, sender=user, body="hello")
        # emit fired (synchronous, OUTBOX off) and the row exists — same txn.
        events = [e for e in captured_events if e.event_type == "chat.message"]
        assert any(e.payload["message_id"] == str(msg.id) for e in events)
        assert Message.objects.filter(pk=msg.id).exists()

    def test_emit_payload_matches_schema(self, user, captured_events):
        conv = services.create_group(owner=user)
        # If the payload drifted from schemas/emits/chat.message.json, the
        # VALIDATE_SCHEMAS-on emit() would raise here.
        services.post_message(conversation=conv, sender=user, body="ok")
        assert any(e.event_type == "chat.message" for e in captured_events)


def test_emit_check_clean():
    """The static outbox-atomicity gate (stapel_core.lint.emit_check) passes on
    the whole package — every emit is inside mutate_and_emit()/atomic()."""
    result = subprocess.run(
        [sys.executable, "-m", "stapel_core.lint.emit_check", str(REPO)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
