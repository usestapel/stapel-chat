"""Attachment URLs are resolved per read, never replayed from the row.

A private CDN object (a voice note, a document) is served by a signed link
that expires. These tests stand a fake ``cdn.describe_many`` in front of the
module that mints a NEW link on every call, so "the URL a reader got was
resolved for that read" is observable: a URL that came from the row would
repeat, and one from a cache would repeat inside the cache window.
"""
from __future__ import annotations

import itertools

import pytest
from stapel_core.comm import function, function_registry

from stapel_chat import services
from stapel_chat.models import Message

pytestmark = pytest.mark.django_db

VOICE = "audio/" + "a1" * 32
DOC = "file/" + "d0" * 32


class FakeCdn:
    """``cdn.describe_many`` that signs a fresh link per call."""

    def __init__(self, missing=(), fail=False):
        self.calls: list[list[str]] = []
        self.missing = set(missing)
        self.fail = fail
        self._token = itertools.count(1)

    def install(self):
        function_registry._providers.pop("cdn.describe_many", None)

        @function("cdn.describe_many")
        def _describe_many(payload):
            refs = list(payload["refs"])
            self.calls.append(refs)
            if self.fail:
                raise ConnectionError("cdn down")
            token = next(self._token)
            items = {}
            for ref in refs:
                if ref in self.missing:
                    continue
                kind = ref.split("/")[0]
                items[ref] = {
                    "ref": ref,
                    "kind": kind,
                    "mime": "audio/ogg" if kind == "audio" else "application/pdf",
                    "bytes": 2048,
                    "duration_ms": 4200 if kind == "audio" else None,
                    "meta_status": "ok",
                    "meta_reason": None,
                    "variants": [{
                        "tier": "original", "branch": None,
                        "url": f"/cdn/api/v1/media/signed/{ref}-t{token}/",
                        "width": None, "height": None,
                    }],
                }
            return {"items": items, "missing": [r for r in refs if r in self.missing]}

        return self


def _send(conv, sender, *keys):
    return services.post_message(
        conversation=conv,
        sender=sender,
        body="",
        attachments=[{"key": k, "type": k.split("/")[0]} for k in keys],
    )


def _history(client, conv):
    r = client.get(f"/chat/api/v1/conversations/{conv.id}/messages")
    assert r.status_code == 200, r.content
    return r.json()["items"]


def _url(attachment):
    [variant] = attachment["variants"]
    return variant["url"]


class TestStoredForm:
    def test_the_row_keeps_the_ref_and_the_facts_but_no_url(self, cdn, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = _send(conv, user, VOICE)
        [stored] = Message.objects.get(pk=msg.pk).attachments
        assert stored["key"] == VOICE
        assert stored["duration_ms"] == 4200 and stored["bytes"] == 2048
        assert "variants" not in stored and "poster_url" not in stored
        assert "/media/" not in repr(stored)

    def test_a_url_the_sender_claims_is_not_stored(self, cdn, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = services.post_message(
            conversation=conv, sender=user, body="",
            attachments=[{
                "key": VOICE, "type": "audio", "poster_url": "https://x.example/p",
                "variants": [{"tier": "original", "url": "https://x.example/a"}],
            }],
        )
        assert "x.example" not in repr(Message.objects.get(pk=msg.pk).attachments)


class TestReadResolves:
    def test_every_read_gets_a_url_resolved_for_it(self, cdn, auth_client, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        _send(conv, other_user, VOICE)
        first = _url(_history(auth_client, conv)[0]["attachments"][0])
        second = _url(_history(auth_client, conv)[0]["attachments"][0])
        assert first != second, "a URL replayed from the row would repeat"
        assert first.startswith("/cdn/api/v1/media/signed/")

    def test_a_page_costs_one_batched_describe(self, cdn, auth_client, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        for i in range(6):
            _send(conv, other_user, "audio/" + f"{i:02d}" * 32, "file/" + f"{i:02d}" * 32)
        cdn.calls.clear()
        items = _history(auth_client, conv)
        assert len(items) == 6
        assert len(cdn.calls) == 1, "one describe for the whole page, not per message"
        assert len(cdn.calls[0]) == 12

    def test_the_cache_serves_a_snapshot_for_less_than_its_window(
        self, cdn, settings, auth_client, user, other_user
    ):
        settings.STAPEL_CHAT = {"ATTACHMENT_URL_CACHE_S": 30}
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        _send(conv, other_user, VOICE)
        cdn.calls.clear()
        first = _url(_history(auth_client, conv)[0]["attachments"][0])
        second = _url(_history(auth_client, conv)[0]["attachments"][0])
        assert first == second
        assert len(cdn.calls) <= 1

    def test_a_row_written_with_a_baked_url_reads_like_a_new_one(
        self, cdn, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = _send(conv, other_user, VOICE)
        Message.objects.filter(pk=msg.pk).update(attachments=[{
            "key": VOICE, "type": "audio", "meta_status": "ok", "duration_ms": 4200,
            "variants": [{"tier": "original", "url": "/media/private/audio/old.ogg"}],
            "poster_url": None,
        }])
        [attachment] = _history(auth_client, conv)[0]["attachments"]
        assert _url(attachment).startswith("/cdn/api/v1/media/signed/")
        assert "old.ogg" not in repr(attachment)

    def test_an_unanswered_describe_degrades_the_item_not_the_thread(
        self, cdn, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        _send(conv, other_user, VOICE)
        services.post_message(conversation=conv, sender=other_user, body="still here")
        cdn.fail = True
        items = _history(auth_client, conv)
        assert [m["body"] for m in items if m["body"]] == ["still here"]
        [attachment] = next(m for m in items if m["attachments"])["attachments"]
        assert attachment["variants"] == []
        assert attachment["meta_reason"] == "cdn_unavailable"
        assert attachment["duration_ms"] == 4200, "stored facts still render"

    def test_a_deleted_cdn_object_is_missing_for_that_item(
        self, cdn, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        _send(conv, other_user, VOICE, DOC)
        cdn.missing = {DOC}
        voice, doc = _history(auth_client, conv)[0]["attachments"]
        assert voice["meta_status"] == "ok" and _url(voice)
        assert doc["meta_status"] == "missing" and doc["meta_reason"] == "unknown_ref"
        assert doc["variants"] == []

    def test_the_send_response_and_an_edit_carry_resolved_urls(
        self, cdn, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        r = auth_client.post(
            f"/chat/api/v1/conversations/{conv.id}/messages",
            {"body": "hi", "attachments": [{"key": VOICE, "type": "audio"}]},
            format="json",
        )
        assert r.status_code == 201
        assert _url(r.json()["attachments"][0]).startswith("/cdn/api/v1/media/signed/")
        r = auth_client.patch(
            f"/chat/api/v1/conversations/{conv.id}/messages/{r.json()['id']}",
            {"body": "hi!"}, format="json",
        )
        assert r.status_code == 200
        assert _url(r.json()["attachments"][0]).startswith("/cdn/api/v1/media/signed/")


class TestRealtimeResolves:
    def test_the_live_frame_carries_urls_resolved_at_push(
        self, cdn, monkeypatch, user, other_user, django_capture_on_commit_callbacks
    ):
        from stapel_chat import realtime

        frames = []
        monkeypatch.setattr(
            realtime, "_deliver_frame",
            lambda stream, payload, seq: frames.append(payload) or True,
        )
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        with django_capture_on_commit_callbacks(execute=True):
            _send(conv, user, VOICE)
        [frame] = frames
        assert _url(frame["attachments"][0]).startswith("/cdn/api/v1/media/signed/")

    def test_a_replay_resolves_the_whole_batch_at_once(self, cdn, user, other_user):
        from stapel_chat.consumers import _replay

        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        for i in range(3):
            _send(conv, other_user, "audio/" + f"{i:02d}" * 32)
        cdn.calls.clear()
        rows = _replay(conv.id, 0, 50, user_id=user.id)
        assert len(rows) == 3
        assert len(cdn.calls) == 1
        for row in rows:
            assert _url(row.payload["attachments"][0]).startswith(
                "/cdn/api/v1/media/signed/"
            )
