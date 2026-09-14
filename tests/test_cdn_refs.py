"""Chat claims the CDN media its messages point at — and releases it.

The defect this file pins down: stapel-cdn stamps ``unreferenced_since`` at
upload and its hourly sweeper reaps anything still zero-ref after
``UNCLAIMED_TTL_HOURS`` — bytes AND row, unrecoverably. A module that stores a
CDN ref and never claims it is therefore handing its users a lease, not an
attachment. Listings and profiles claim through
``stapel_core.django.cdn.ref_sync.sync_cdn_refs``; this module did not, so
every chat photo and voice note was on a 48-hour clock.

What is asserted here is the CALL and its arguments — service, entity type,
entity id, the old set and the new one. "It did not raise" proves nothing
about a reference counter on another machine.

The one that is easy to get backwards, and destroys the counterparty's media
if you do: **``clear_conversation`` releases nothing.** It is a per-viewer
cursor (``cleared_at``), not a deletion — the other party still sees the
message, so its attachment must outlive the clear.
"""
import pytest

from stapel_chat import services
from stapel_chat.models import Conversation, Message

pytestmark = pytest.mark.django_db


IMAGE = {"key": "product/h1", "type": "image"}
VOICE = {"key": "audio/h2", "type": "audio"}


@pytest.fixture
def capture_sync(monkeypatch):
    """Record every ``sync_cdn_refs`` call as
    ``(service, entity_type, entity_id, old, new)`` with the ref sets
    normalized, so an assertion reads as the claim it is."""
    import stapel_core.django.cdn.ref_sync as ref_sync

    calls = []

    def fake_sync(service, entity_type, entity_id, old_refs, new_refs):
        calls.append(
            (service, entity_type, str(entity_id), set(old_refs), set(new_refs))
        )
        return ref_sync.RefSyncResult(ok=True)

    monkeypatch.setattr(ref_sync, "sync_cdn_refs", fake_sync)
    return calls


@pytest.fixture
def commit(django_capture_on_commit_callbacks):
    """Run ``on_commit`` work inside the test's transaction.

    The claim is published post-commit on purpose (never under the
    conversation lock), which in a ``django_db`` test means it never runs
    unless the callbacks are executed.
    """
    def _run():
        return django_capture_on_commit_callbacks(execute=True)

    return _run


def _direct(user, other):
    return services.create_direct(owner=user, other_user_id=other.id)


def _post(conv, sender, commit, **kwargs):
    with commit():
        return services.post_message(conversation=conv, sender=sender, **kwargs)


class TestClaimOnCreate:
    def test_a_message_with_attachments_claims_every_ref(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        msg = _post(conv, user, commit, body="look", attachments=[IMAGE, VOICE])
        assert capture_sync == [
            ("chat", "message", str(msg.id), set(), {"product/h1", "audio/h2"})
        ]

    def test_a_message_without_attachments_publishes_nothing(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        _post(conv, user, commit, body="just words")
        assert capture_sync == []

    def test_an_idempotent_resend_does_not_claim_twice(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        first = _post(
            conv, user, commit, body="hi", attachments=[IMAGE], client_msg_id="c-1"
        )
        capture_sync.clear()
        again = _post(
            conv, user, commit, body="hi", attachments=[IMAGE], client_msg_id="c-1"
        )
        assert again.pk == first.pk
        assert capture_sync == []

    def test_the_claim_is_published_outside_the_conversation_lock(
        self, user, other_user, capture_sync, commit
    ):
        """The publish is post-commit work, not part of the locked section.

        Asserted by its absence before the callbacks run: if the sync were
        called inline inside ``_post_once`` it would already be recorded
        while the conversation row was still locked.
        """
        conv = _direct(user, other_user)
        with commit() as callbacks:
            services.post_message(
                conversation=conv, sender=user, body="x", attachments=[IMAGE]
            )
            assert capture_sync == [], "claimed while the conversation was locked"
        assert callbacks, "no post-commit work was scheduled"
        assert len(capture_sync) == 1


class TestSyncOldToNew:
    def test_replacing_the_attachment_set_syncs_old_to_new(
        self, user, other_user, capture_sync, commit
    ):
        """A moved claim releases the old refs and claims the new ones in one
        event — ``apply_ref_sync`` derives both sets by difference.

        There is no attachment-edit affordance in this module today
        (``edit_message`` replaces the body only), so this exercises the seam
        any future one has to go through.
        """
        conv = _direct(user, other_user)
        msg = _post(conv, user, commit, body="v1", attachments=[IMAGE])
        capture_sync.clear()
        with commit():
            services._schedule_message_cdn_ref_sync(
                msg.id, {"product/h1"}, {"audio/h2"}
            )
        assert capture_sync == [
            ("chat", "message", str(msg.id), {"product/h1"}, {"audio/h2"})
        ]

    def test_an_unmoved_set_publishes_nothing(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        msg = _post(conv, user, commit, body="v1", attachments=[IMAGE])
        capture_sync.clear()
        with commit():
            services._schedule_message_cdn_ref_sync(
                msg.id, {"product/h1"}, {"product/h1"}
            )
        assert capture_sync == []


class TestReleaseOnTombstone:
    def test_a_tombstone_releases_every_ref(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        msg = _post(conv, user, commit, body="oops", attachments=[IMAGE, VOICE])
        capture_sync.clear()
        with commit():
            services.delete_message(message=msg, actor=user)
        assert capture_sync == [
            ("chat", "message", str(msg.id), {"product/h1", "audio/h2"}, set())
        ]

    def test_deleting_an_already_deleted_message_releases_once(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        msg = _post(conv, user, commit, body="oops", attachments=[IMAGE])
        capture_sync.clear()
        with commit():
            services.delete_message(message=msg, actor=user)
        with commit():
            services.delete_message(message=msg, actor=user)
        assert len(capture_sync) == 1

    def test_deleting_a_message_without_attachments_publishes_nothing(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        msg = _post(conv, user, commit, body="words")
        capture_sync.clear()
        with commit():
            services.delete_message(message=msg, actor=user)
        assert capture_sync == []


class TestReleaseOnErasure:
    def test_gdpr_erasure_releases_every_ref_the_user_claimed(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        mine = _post(conv, user, commit, body="mine", attachments=[IMAGE])
        theirs = _post(conv, other_user, commit, body="theirs", attachments=[VOICE])
        capture_sync.clear()
        with commit():
            services.erase_user_messages(user.id)
        assert capture_sync == [
            ("chat", "message", str(mine.id), {"product/h1"}, set())
        ], "erasure releases the erased author's media and nobody else's"
        theirs.refresh_from_db()
        assert theirs.attachments, "the counterparty's message is untouched"

    def test_deleting_a_dead_direct_thread_releases_what_it_cascades(
        self, user, other_user, capture_sync, commit
    ):
        """The GDPR provider deletes a direct thread left with one
        participant, cascading messages nobody erased — including the
        counterparty's, whose refs would otherwise be pinned forever with no
        entity left to release them."""
        from stapel_chat.gdpr import ChatGDPRProvider

        conv = _direct(user, other_user)
        theirs = _post(conv, other_user, commit, body="theirs", attachments=[VOICE])
        capture_sync.clear()
        with commit():
            ChatGDPRProvider().delete(user.id)
        assert not Conversation.objects.filter(pk=conv.pk).exists()
        assert ("chat", "message", str(theirs.id), {"audio/h2"}, set()) in capture_sync

    def test_a_delete_that_fails_releases_nothing(
        self, user, other_user, capture_sync, commit, monkeypatch
    ):
        """The release is published AFTER the row goes, never before.

        The GDPR provider runs in no transaction of its own, so a release
        published first is published immediately — and a delete that then
        failed would have handed the sweeper media a live message still points
        at. That is this defect, not a smaller version of it.
        """
        conv = _direct(user, other_user)
        msg = _post(conv, user, commit, body="mine", attachments=[IMAGE])
        capture_sync.clear()

        def boom(*args, **kwargs):
            raise RuntimeError("the cascade failed")

        monkeypatch.setattr(Conversation, "delete", boom)
        with pytest.raises(RuntimeError):
            with commit():
                services._delete_conversation_row(conv)
        assert capture_sync == [], "released media a surviving message claims"
        assert Message.objects.filter(pk=msg.pk).exists()


class TestClearConversationReleasesNothing:
    """``clear_conversation`` is a per-viewer cursor. Releasing there would
    reap media the OTHER party is still being served."""

    def test_clearing_my_own_history_releases_nothing(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        msg = _post(conv, other_user, commit, body="theirs", attachments=[IMAGE])
        capture_sync.clear()
        with commit():
            services.clear_conversation(conversation=conv, user=user)
        assert capture_sync == []
        msg.refresh_from_db()
        assert [a["key"] for a in msg.attachments] == ["product/h1"]

    def test_the_counterparty_still_sees_the_attachment(
        self, user, other_user, capture_sync, commit
    ):
        conv = _direct(user, other_user)
        _post(conv, other_user, commit, body="theirs", attachments=[IMAGE])
        with commit():
            services.clear_conversation(conversation=conv, user=user)
        visible = services.visible_messages(
            Message.objects.filter(conversation=conv),
            cleared_at=services.cleared_at_of(conv, other_user),
        )
        assert [a["key"] for m in visible for a in m.attachments] == ["product/h1"]


class TestBackfill:
    """``chat_backfill_cdn_refs`` — the pass over messages written before
    claim-on-send. Additive by construction (``old_hashes=[]``), so a rerun
    adds nothing and releases nothing."""

    def _legacy(self, conv, sender, attachments, **fields):
        """A row shaped like pre-claim data: attachments present, nothing ever
        claimed — inserted around ``post_message`` so no claim is published."""
        seq = Conversation.objects.filter(pk=conv.pk).values_list(
            "last_seq", flat=True
        )[0] + 1
        Conversation.objects.filter(pk=conv.pk).update(last_seq=seq)
        return Message.objects.create(
            conversation=conv,
            sender=sender,
            seq=seq,
            rev_seq=seq,
            attachments=attachments,
            **fields,
        )

    def test_claims_every_live_message_that_carries_refs(
        self, user, other_user, capture_sync
    ):
        from stapel_chat.cdn_refs_backfill import backfill_cdn_refs

        conv = _direct(user, other_user)
        msg = self._legacy(conv, user, [IMAGE, VOICE])
        stats = backfill_cdn_refs()
        assert capture_sync == [
            ("chat", "message", str(msg.id), set(), {"product/h1", "audio/h2"})
        ]
        assert stats == {"candidates": 1, "published": 1, "failed": 0}

    def test_skips_messages_without_attachments(
        self, user, other_user, capture_sync
    ):
        from stapel_chat.cdn_refs_backfill import backfill_cdn_refs

        conv = _direct(user, other_user)
        self._legacy(conv, user, [])
        assert backfill_cdn_refs() == {
            "candidates": 0, "published": 0, "failed": 0
        }
        assert capture_sync == []

    def test_skips_tombstones(self, user, other_user, capture_sync):
        """A deleted message released its refs; re-claiming them here would
        undo the release and pin media the tombstone let go."""
        from django.utils import timezone

        from stapel_chat.cdn_refs_backfill import backfill_cdn_refs

        conv = _direct(user, other_user)
        self._legacy(conv, user, [IMAGE], deleted_at=timezone.now())
        assert backfill_cdn_refs()["candidates"] == 0
        assert capture_sync == []

    def test_is_additive_and_rerunnable(self, user, other_user, capture_sync):
        from stapel_chat.cdn_refs_backfill import backfill_cdn_refs

        conv = _direct(user, other_user)
        self._legacy(conv, user, [IMAGE])
        backfill_cdn_refs()
        backfill_cdn_refs()
        assert len(capture_sync) == 2
        assert all(old == set() for _, _, _, old, _ in capture_sync), (
            "every backfill event must be additive — an old set would RELEASE"
        )

    def test_dry_run_counts_and_publishes_nothing(
        self, user, other_user, capture_sync
    ):
        from stapel_chat.cdn_refs_backfill import backfill_cdn_refs

        conv = _direct(user, other_user)
        self._legacy(conv, user, [IMAGE])
        stats = backfill_cdn_refs(dry_run=True)
        assert stats == {"candidates": 1, "published": 0, "failed": 0}
        assert capture_sync == []

    def test_limit_bounds_the_pass(self, user, other_user, capture_sync):
        from stapel_chat.cdn_refs_backfill import backfill_cdn_refs

        conv = _direct(user, other_user)
        self._legacy(conv, user, [IMAGE])
        self._legacy(conv, user, [VOICE])
        assert backfill_cdn_refs(limit=1)["candidates"] == 1
        assert len(capture_sync) == 1

    def test_a_failed_publish_is_counted_not_raised(
        self, user, other_user, monkeypatch
    ):
        import stapel_core.django.cdn.ref_sync as ref_sync

        from stapel_chat.cdn_refs_backfill import backfill_cdn_refs

        conv = _direct(user, other_user)
        self._legacy(conv, user, [IMAGE])
        monkeypatch.setattr(
            ref_sync,
            "sync_cdn_refs",
            lambda *a, **k: ref_sync.RefSyncResult(ok=False, errors=["bus down"]),
        )
        assert backfill_cdn_refs() == {
            "candidates": 1, "published": 0, "failed": 1
        }

    def test_the_management_command_runs(self, user, other_user, capture_sync):
        from django.core.management import call_command

        conv = _direct(user, other_user)
        self._legacy(conv, user, [IMAGE])
        call_command("chat_backfill_cdn_refs", "--dry-run")
        assert capture_sync == []
        call_command("chat_backfill_cdn_refs")
        assert len(capture_sync) == 1

    def test_the_command_package_ships_in_the_wheel(self):
        """A management command that is not in ``[tool.setuptools].packages``
        exists in the source tree and in nobody's install — the operator runs
        the backfill on the stand, not here."""
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        packages = tomllib.loads(pyproject.read_text(encoding="utf-8"))[
            "tool"
        ]["setuptools"]["packages"]
        assert "stapel_chat.management" in packages
        assert "stapel_chat.management.commands" in packages
