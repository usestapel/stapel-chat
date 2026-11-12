"""A sender may attach only private objects they own — asked of the CDN.

Every read resolves an attachment's URLs for the thread's readers, so
attaching a ref shares its bytes. ``cdn.ownership`` (stapel-cdn 0.31.0+)
answers whether the sender holds each private ref; a CDN without it leaves
sends working, counted and logged as unverified (``stapel_chat.W006``).
"""
from __future__ import annotations

import logging

import pytest
from django.core.checks import run_checks
from rest_framework.test import APIClient
from stapel_core.comm import function, function_registry

from stapel_chat import attachments, services
from stapel_chat.consumers import ERROR_ATTACHMENT_NOT_OWNED, _do_send
from stapel_chat.models import Message

pytestmark = pytest.mark.django_db

MINE = "file/" + "a1" * 32
THEIRS = "file/" + "b2" * 32
VOICE_THEIRS = "audio/" + "c3" * 32
PHOTO = "avatar/" + "d4" * 32
GHOST = "file/" + "e5" * 32
NOT_OWNED = "error.403.chat_attachment_not_owned"


class FakeOwnership:
    """``cdn.ownership`` over a fixed table ``{ref: (owner_id, visibility)}``."""

    def __init__(self, table, fail=False, drop=()):
        self.table = table
        self.fail = fail
        self.drop = set(drop)
        self.calls: list[dict] = []

    def install(self):
        function_registry._providers.pop("cdn.ownership", None)

        @function("cdn.ownership")
        def _ownership(payload):
            self.calls.append(payload)
            if self.fail:
                raise ConnectionError("cdn down")
            principal = payload.get("owner_id")
            items = []
            for ref in payload["refs"]:
                if ref in self.drop:
                    continue
                if ref not in self.table:
                    items.append({"ref": ref, "exists": False, "owner_id": None,
                                  "owned": False, "visibility": None})
                    continue
                owner, visibility = self.table[ref]
                items.append({"ref": ref, "exists": True, "owner_id": owner,
                              "owned": principal == owner, "visibility": visibility})
            return {"items": items}

        return self


@pytest.fixture
def ownership(user, other_user):
    return FakeOwnership({
        MINE: (str(user.pk), "private"),
        THEIRS: (str(other_user.pk), "private"),
        VOICE_THEIRS: (str(other_user.pk), "private"),
        PHOTO: (str(other_user.pk), "public"),
    }).install()


@pytest.fixture
def thread(user, other_user):
    return services.create_direct(owner=user, other_user_id=other_user.id)


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _type(ref):
    prefix = ref.split("/")[0]
    return "image" if prefix == "avatar" else prefix


def _post(user, conv, *refs):
    return _client(user).post(
        f"/chat/api/v1/conversations/{conv.id}/messages",
        {"body": "", "attachments": [{"key": r, "type": _type(r)} for r in refs]},
        format="json",
    )


def _refused_at(response, index):
    assert response.status_code == 403, response.content
    body = response.json()
    assert body["localizable_error"] == NOT_OWNED
    assert body["params"] == {"index": index}


class TestRefusal:
    def test_my_own_private_file_sends(self, cdn, ownership, user, thread):
        assert _post(user, thread, MINE).status_code == 201
        assert ownership.calls == [{"refs": [MINE], "owner_id": str(user.pk)}]

    def test_someone_elses_private_file_is_refused_by_index(self, cdn, ownership, user, thread):
        _refused_at(_post(user, thread, MINE, THEIRS), 1)
        assert not Message.objects.filter(conversation=thread).exists()

    def test_a_ref_that_does_not_exist_is_refused_the_same_way(
        self, cdn, ownership, user, thread
    ):
        _refused_at(_post(user, thread, GHOST), 0)

    def test_a_public_object_is_shareable_by_anyone(self, cdn, ownership, user, thread):
        assert _post(user, thread, PHOTO).status_code == 201

    def test_one_call_per_message(self, cdn, ownership, user, thread):
        _post(user, thread, MINE, PHOTO, MINE)
        assert len(ownership.calls) == 1
        assert ownership.calls[0]["refs"] == [MINE, PHOTO]

    def test_the_socket_refuses_with_its_own_code(self, cdn, ownership, user, thread):
        err = _do_send(
            thread.id, user.pk,
            {"body": "", "attachments": [{"key": VOICE_THEIRS, "type": "audio"}]},
        )
        assert err["code"] == ERROR_ATTACHMENT_NOT_OWNED
        assert "attachment 0" in err["message"]
        assert not Message.objects.filter(conversation=thread).exists()

    def test_a_system_line_is_not_checked(self, cdn, ownership, thread):
        services.post_message(conversation=thread, sender=None, kind="system", body="x")
        assert ownership.calls == []

    def test_client_metadata_mode_asks_nothing(self, ownership, user, thread, settings):
        settings.STAPEL_CHAT = {"ATTACHMENT_METADATA": "client"}
        assert _post(user, thread, THEIRS).status_code == 201
        assert ownership.calls == []


class TestAlreadyInTheThread:
    def test_a_participant_may_quote_a_file_already_shared_here(
        self, cdn, ownership, user, other_user, thread
    ):
        assert _post(other_user, thread, THEIRS).status_code == 201
        assert _post(user, thread, THEIRS).status_code == 201

    def test_a_deleted_message_shares_nothing(self, cdn, ownership, user, other_user, thread):
        assert _post(other_user, thread, THEIRS).status_code == 201
        msg = Message.objects.get(conversation=thread)
        services.delete_message(message=msg, actor=other_user)
        _refused_at(_post(user, thread, THEIRS), 0)

    def test_another_thread_shares_nothing(self, cdn, ownership, user, other_user, thread):
        elsewhere = services.create_group(owner=other_user, participant_ids=[])
        services.post_message(
            conversation=elsewhere, sender=other_user, body="",
            attachments=[{"key": THEIRS, "type": "file"}],
        )
        _refused_at(_post(user, thread, THEIRS), 0)


class TestSupport:
    def test_the_assigned_operator_forwards_the_customers_file(
        self, cdn, user, operator_user
    ):
        FakeOwnership({MINE: (str(user.pk), "private")}).install()
        conv = services.create_support(customer=user)
        assert _post(user, conv, MINE).status_code == 201
        services.assign_operator(conversation=conv, operator=operator_user)
        assert _post(operator_user, conv, MINE).status_code == 201

    def test_the_operator_cannot_attach_a_stranger_file(
        self, cdn, user, other_user, operator_user
    ):
        FakeOwnership({THEIRS: (str(other_user.pk), "private")}).install()
        conv = services.create_support(customer=user)
        services.assign_operator(conversation=conv, operator=operator_user)
        _refused_at(_post(operator_user, conv, THEIRS), 0)


class TestUnverified:
    """A CDN that cannot answer: sends keep working, and say so."""

    def _sends_and_counts(self, user, thread, caplog, ref=THEIRS):
        before = attachments.ownership_unverified_total()
        with caplog.at_level(logging.WARNING, logger="stapel_chat.attachments"):
            assert _post(user, thread, ref).status_code == 201
        assert attachments.ownership_unverified_total() == before + 1
        assert "ownership NOT verified" in caplog.text

    def test_no_function_as_on_cdn_0_30(self, cdn, user, thread, caplog):
        assert "cdn.ownership" not in function_registry.names()
        self._sends_and_counts(user, thread, caplog)

    def test_a_failing_function(self, cdn, user, thread, caplog):
        FakeOwnership({}, fail=True).install()
        self._sends_and_counts(user, thread, caplog)

    def test_an_answer_that_skips_a_ref(self, cdn, user, other_user, thread, caplog):
        FakeOwnership({THEIRS: (str(other_user.pk), "private")}, drop={THEIRS}).install()
        self._sends_and_counts(user, thread, caplog)


class TestBootCheck:
    def _ids(self):
        return {m.id for m in run_checks()}

    def test_w006_when_the_function_is_absent(self):
        assert "cdn.ownership" not in function_registry.names()
        assert "stapel_chat.W006" in self._ids()

    def test_silent_once_the_function_is_there(self, ownership):
        assert "stapel_chat.W006" not in self._ids()

    def test_silent_in_client_mode(self, settings):
        settings.STAPEL_CHAT = {"ATTACHMENT_METADATA": "client"}
        assert "stapel_chat.W006" not in self._ids()
