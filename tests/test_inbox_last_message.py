"""The line an inbox row draws under the title — `last_message`.

`ConversationResponse` carried `last_seq` and no message, so a conversation
list could say a thread had moved but not what it said: the client either drew
an empty row or spent a request per row (`GET /messages?limit=1`, fifty of them
for a fifty-row inbox) to fill it in. These tests pin the projection, and — the
part that is easy to get wrong — that WHAT a row previews is the same rule that
decides what `?search=` finds on the last line. Two rules would mean a row found
by a word its own preview does not contain, and a row whose preview nobody can
search for.
"""
import pytest

from stapel_chat import services
from stapel_chat.models import Conversation

pytestmark = pytest.mark.django_db

LIST = "/chat/api/v1/conversations"


def _user(username):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="x"
    )


def _row(auth_client, conv):
    for row in auth_client.get(LIST).json()["items"]:
        if row["id"] == str(conv.id):
            return row
    raise AssertionError(f"{conv.id} is not in the list")


def _ids(response):
    return [row["id"] for row in response.json()["items"]]


# ── What a row now carries ───────────────────────────────────────────────


class TestTheProjection:
    def test_every_row_paints_its_last_line_without_opening_the_thread(
        self, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(conversation=conv, sender=other_user, body="older one")
        last = services.post_message(
            conversation=conv, sender=other_user, body="Is the bicycle still there?"
        )

        row = _row(auth_client, conv)
        assert row["last_message"] == {
            "seq": last.seq,
            "kind": "text",
            "sender_id": str(other_user.id),
            "created_at": row["last_message"]["created_at"],
            "attachment_types": [],
            "attachment_count": 0,
            "body_preview": "Is the bicycle still there?",
            "preview_reason": None,
        }
        # The row's own high-water mark and the preview's seq are the same
        # message — that is how a client knows the line it holds is current.
        assert row["last_seq"] == last.seq

    def test_a_thread_nobody_has_written_in_has_no_last_message(
        self, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        assert _row(auth_client, conv)["last_message"] is None

    def test_a_tombstone_previews_as_nothing_and_says_so(
        self, auth_client, user, other_user
    ):
        """The row still knows a message is there — seq, kind, author, time —
        and draws no words for it. Shipping the withdrawn body as a preview is
        the one thing a tombstone exists to prevent."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        doomed = services.post_message(
            conversation=conv, sender=other_user, body="withdrawn text"
        )
        services.delete_message(message=doomed, actor=other_user)

        last = _row(auth_client, conv)["last_message"]
        assert last["seq"] == doomed.seq
        assert last["kind"] == "text"
        assert last["sender_id"] == str(other_user.id)
        assert last["body_preview"] is None
        assert last["preview_reason"] == "deleted"

    def test_the_preview_is_one_plain_line_and_never_longer_than_140(
        self, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(
            conversation=conv,
            sender=other_user,
            body="first line\n\n   second line  \t third\n" + "x" * 300,
        )

        preview = _row(auth_client, conv)["last_message"]["body_preview"]
        assert len(preview) <= services.PREVIEW_MAX_CHARS
        assert "\n" not in preview and "\t" not in preview
        assert preview.startswith("first line second line third x")
        assert preview.endswith("…")

    def test_the_single_conversation_read_carries_the_same_line(
        self, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(conversation=conv, sender=other_user, body="hello there")

        detail = auth_client.get(f"{LIST}/{conv.id}").json()
        assert detail["last_message"] == _row(auth_client, conv)["last_message"]

    def test_an_unannotated_conversation_costs_exactly_one_read(
        self, user, other_user, django_assert_num_queries
    ):
        """A caller that hands `conversation_to_dto` a conversation nobody
        annotated pays ONE query for the line — the same fallback the unread
        count has. A page never takes this path (see the query-count test in
        tests/test_inbox_search.py); a caller holding one object may."""
        from stapel_chat.views import last_message_to_dto

        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(conversation=conv, sender=other_user, body="one line")

        plain = Conversation.objects.get(pk=conv.pk)
        with django_assert_num_queries(1):
            projected = last_message_to_dto(plain)
        assert projected.body_preview == "one line"

        # …and nothing at all for a thread with no messages: last_seq says so.
        empty = Conversation.objects.get(
            pk=services.create_direct(owner=user, other_user_id=_user("quiet").id).pk
        )
        with django_assert_num_queries(0):
            assert last_message_to_dto(empty) is None


# ── System lines: the module owns the marker, the host owns the words ────


class TestSystemLines:
    def test_a_marker_this_deployment_gave_no_words_draws_none(
        self, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_system_message(
            conversation_id=str(conv.id), body="video.call.ended:188"
        )

        last = _row(auth_client, conv)["last_message"]
        assert last["kind"] == "system"
        assert last["sender_id"] is None
        # Not the marker. `video.call.ended:188` on a conversation list is
        # machine vocabulary shown to a person; `kind` is what the client
        # renders its own phrase from.
        assert last["body_preview"] is None
        assert last["preview_reason"] == "system"

    def test_a_labelled_marker_draws_its_label(
        self, auth_client, user, other_user, settings
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_system_message(
            conversation_id=str(conv.id), body="chat.support.resolved"
        )
        settings.STAPEL_CHAT = {
            "SYSTEM_LINE_LABELS": {"chat.support.resolved": "Ticket resolved"}
        }
        last = _row(auth_client, conv)["last_message"]
        assert last["body_preview"] == "Ticket resolved"
        # A labelled marker draws words, so there is nothing left to explain.
        assert last["preview_reason"] is None

    def test_the_label_is_found_on_the_marker_not_on_its_argument(
        self, auth_client, user, other_user, settings
    ):
        """A marker may carry an argument (`video.call.ended:188`). The label
        is looked up on the marker; the argument is never interpolated, because
        a module that wrote "188" into a sentence would be inventing the
        sentence."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_system_message(
            conversation_id=str(conv.id), body="video.call.ended:188"
        )
        settings.STAPEL_CHAT = {
            "SYSTEM_LINE_LABELS": {"video.call.ended": "Call ended"}
        }
        assert _row(auth_client, conv)["last_message"]["body_preview"] == "Call ended"

    def test_a_deleted_system_line_draws_nothing_either(
        self, auth_client, user, other_user, settings
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        line = services.post_system_message(
            conversation_id=str(conv.id), body="chat.support.resolved"
        )
        settings.STAPEL_CHAT = {
            "SYSTEM_LINE_LABELS": {"chat.support.resolved": "Ticket resolved"}
        }
        # A system line is not deletable through the API (edit/delete refuse
        # non-text kinds); erasure reaches one, so the tombstone is stamped
        # here the way erase_user_messages would leave it.
        from django.utils import timezone

        from stapel_chat.models import Message

        Message.objects.filter(pk=line.pk).update(deleted_at=timezone.now())
        last = _row(auth_client, conv)["last_message"]
        assert last["body_preview"] is None
        # Deletion wins over a label: a withdrawn system line reads as
        # "deleted", not as "system" — the labelled word is what was withdrawn.
        assert last["preview_reason"] == "deleted"


class TestPreviewReason:
    """`preview_reason` names which of the three `body_preview: null` cases a
    row is in, so a client that wants "Message deleted" rather than the
    "Attachment" it would otherwise default to can tell the difference."""

    def test_an_attachment_only_message_previews_as_nothing_and_says_so(
        self, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = services.post_message(
            conversation=conv,
            sender=other_user,
            body="",
            attachments=[{"key": "product/abc", "type": "image"}],
        )

        last = _row(auth_client, conv)["last_message"]
        assert last["seq"] == msg.seq
        assert last["body_preview"] is None
        assert last["preview_reason"] == "attachment"

    def test_a_row_with_words_reports_no_reason(
        self, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(conversation=conv, sender=other_user, body="hello")

        last = _row(auth_client, conv)["last_message"]
        assert last["body_preview"] == "hello"
        assert last["preview_reason"] is None

    def test_the_rule_itself(self):
        assert (
            services.last_line_reason(
                kind="text", body="hi", deleted=False, has_attachments=False
            )
            is None
        )
        assert (
            services.last_line_reason(
                kind="text", body="hi", deleted=True, has_attachments=False
            )
            == "deleted"
        )
        assert (
            services.last_line_reason(
                kind="text", body="", deleted=False, has_attachments=True
            )
            == "attachment"
        )
        assert (
            services.last_line_reason(
                kind="system", body="x.y", deleted=False, has_attachments=False
            )
            == "system"
        )
        # Deletion is checked first: a withdrawn attachment-only message, or a
        # withdrawn system line, both read as "deleted" — never as the case
        # that would have applied had it not been withdrawn.
        assert (
            services.last_line_reason(
                kind="text", body="", deleted=True, has_attachments=True
            )
            == "deleted"
        )
        assert (
            services.last_line_reason(
                kind="system", body="x.y", deleted=True, has_attachments=False
            )
            == "deleted"
        )


# ── Which kind of attachment, not just "some" ────────────────────────────


class TestAttachmentMarks:
    """`preview_reason: "attachment"` says the last line is a file rather than
    words, and nothing about WHICH — so an inbox drew one generic clip for a
    photo, a voice note and a PDF alike. `attachment_types` /
    `attachment_count` are the per-type marks, read from the message's own
    stored descriptors in the query the list already runs.
    """

    def test_a_photo_row_says_photo(self, auth_client, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(
            conversation=conv,
            sender=other_user,
            body="",
            attachments=[{"key": "product/abc", "type": "image"}],
        )

        last = _row(auth_client, conv)["last_message"]
        assert last["attachment_types"] == ["image"]
        assert last["attachment_count"] == 1

    def test_distinct_types_in_order_of_appearance_and_a_total_count(
        self, auth_client, user, other_user
    ):
        """Six attachments, three kinds: three icons and a count of six.

        Distinct, because two photos are one kind of row; in order of
        appearance, because the first icon should be the attachment the
        bubble leads with; and the count is the TOTAL, because that is what
        a `+N` next to the icons has to be counting.
        """
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(
            conversation=conv,
            sender=other_user,
            body="",
            attachments=[
                {"key": "m/1", "type": "video"},
                {"key": "m/2", "type": "image"},
                {"key": "m/3", "type": "image"},
                {"key": "m/4", "type": "file"},
                {"key": "m/5", "type": "image"},
                {"key": "m/6", "type": "video"},
            ],
        )

        last = _row(auth_client, conv)["last_message"]
        assert last["attachment_types"] == ["video", "image", "file"]
        assert last["attachment_count"] == 6

    def test_a_text_row_carries_no_marks(self, auth_client, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(conversation=conv, sender=other_user, body="hello")

        last = _row(auth_client, conv)["last_message"]
        assert last["attachment_types"] == []
        assert last["attachment_count"] == 0

    def test_a_tombstone_draws_no_marks(self, auth_client, user, other_user):
        """A withdrawn message must not leak "and it had three photos"."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = services.post_message(
            conversation=conv,
            sender=other_user,
            body="",
            attachments=[
                {"key": "m/1", "type": "image"},
                {"key": "m/2", "type": "image"},
                {"key": "m/3", "type": "audio"},
            ],
        )
        services.delete_message(message=msg, actor=other_user)

        last = _row(auth_client, conv)["last_message"]
        assert last["preview_reason"] == "deleted"
        assert last["attachment_types"] == []
        assert last["attachment_count"] == 0

    def test_a_body_with_attachments_carries_both_the_words_and_the_marks(
        self, auth_client, user, other_user
    ):
        """The marks are not an alternative to the preview: a captioned photo
        draws its caption AND its picture icon, and `preview_reason` stays
        null because there are words to show."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(
            conversation=conv,
            sender=other_user,
            body="here it is",
            attachments=[{"key": "m/1", "type": "gif"}],
        )

        last = _row(auth_client, conv)["last_message"]
        assert last["body_preview"] == "here it is"
        assert last["preview_reason"] is None
        assert last["attachment_types"] == ["gif"]
        assert last["attachment_count"] == 1

    def test_the_single_conversation_read_carries_the_same_marks(
        self, auth_client, user, other_user
    ):
        """The annotated path and the one-query fallback must agree — two
        rules here would mean a detail header and its own inbox row drawing
        different icons for the same message."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(
            conversation=conv,
            sender=other_user,
            body="",
            attachments=[
                {"key": "m/1", "type": "audio"},
                {"key": "m/2", "type": "file"},
            ],
        )

        detail = auth_client.get(f"{LIST}/{conv.id}").json()["last_message"]
        assert detail == _row(auth_client, conv)["last_message"]
        assert detail["attachment_types"] == ["audio", "file"]
        assert detail["attachment_count"] == 2

    def test_the_marks_cost_no_cdn_call(self, auth_client, user, other_user):
        """Read from the STORED descriptors, never described again. A
        describe per row would put back the fifty requests this whole
        projection exists to delete — and the CDN is not even reachable on
        every deployment that draws an inbox."""
        from stapel_core.comm import function, function_registry

        calls = []
        function_registry._providers.pop("cdn.describe_many", None)

        @function("cdn.describe_many")
        def _describe_many(payload):
            calls.append(list(payload["refs"]))
            return {"items": {}, "missing": []}

        try:
            conv = services.create_direct(owner=user, other_user_id=other_user.id)
            services.post_message(
                conversation=conv,
                sender=other_user,
                body="",
                attachments=[{"key": "m/1", "type": "image"}],
            )
            calls.clear()  # sending may describe; DRAWING THE ROW may not.

            last = _row(auth_client, conv)["last_message"]
            assert last["attachment_types"] == ["image"]
            assert calls == []
        finally:
            function_registry._providers.pop("cdn.describe_many", None)

    def test_the_rule_itself(self):
        rule = services.last_line_attachments

        assert rule(attachments=[], deleted=False) == ((), 0)
        assert rule(attachments=None, deleted=False) == ((), 0)
        assert rule(
            attachments=[{"key": "a", "type": "image"}], deleted=False
        ) == (("image",), 1)
        assert rule(
            attachments=[
                {"key": "a", "type": "image"},
                {"key": "b", "type": "image"},
            ],
            deleted=False,
        ) == (("image",), 2)
        # Deletion wins over whatever the row still stores: rows deleted
        # before tombstones emptied `attachments` are in live databases.
        assert rule(
            attachments=[{"key": "a", "type": "image"}], deleted=True
        ) == ((), 0)
        # The pre-0.3 bare-ref shape, and a descriptor with no type, both read
        # as `file` — the same fallback `normalize_attachment` applies.
        assert rule(attachments=["product/abc"], deleted=False) == (("file",), 1)
        assert rule(attachments=[{"key": "a"}], deleted=False) == (("file",), 1)
        # The registry is OPEN, so a host type travels under its own name
        # rather than being flattened into `file`.
        assert rule(
            attachments=[{"key": "a", "type": "sticker"}], deleted=False
        ) == (("sticker",), 1)


# ── One rule, not two ────────────────────────────────────────────────────


class TestPreviewAndSearchAgree:
    def test_what_a_row_draws_is_what_finds_it(
        self, auth_client, user, other_user
    ):
        """Every word the preview shows finds the row; the words it does NOT
        show (an older message, a withdrawn one) find nothing. This is the
        0.8.2 search rule and the 0.8.3 preview reading one function."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(conversation=conv, sender=other_user, body="parasol")
        services.post_message(
            conversation=conv, sender=other_user, body="the blue bicycle"
        )

        preview = _row(auth_client, conv)["last_message"]["body_preview"]
        assert preview == "the blue bicycle"
        for word in preview.split():
            assert _ids(auth_client.get(LIST, {"search": word})) == [str(conv.id)]
        assert _ids(auth_client.get(LIST, {"search": "parasol"})) == []

    def test_a_thread_that_was_edited_still_has_a_last_line(
        self, auth_client, user, other_user
    ):
        """`Conversation.last_seq` is ALSO the revision counter (one sequence
        serves both roles), so the moment anything in a thread is edited or
        deleted it names a seq no message carries. A last line keyed on that
        number goes blank — and, before 0.8.3, stopped being findable by its
        own last line — for exactly the threads people use most."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = services.post_message(
            conversation=conv, sender=other_user, body="the blue bicycle"
        )
        services.edit_message(
            message=msg, editor=other_user, body="the green bicycle"
        )
        conv.refresh_from_db()
        assert conv.last_seq > msg.seq  # the counter has moved past every seq

        row = _row(auth_client, conv)
        assert row["last_message"]["seq"] == msg.seq
        assert row["last_message"]["body_preview"] == "the green bicycle"
        assert _ids(auth_client.get(LIST, {"search": "green"})) == [str(conv.id)]
        assert _ids(auth_client.get(LIST, {"search": "blue"})) == []

    def test_a_row_that_draws_no_words_is_found_by_none(
        self, auth_client, user, other_user
    ):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        doomed = services.post_message(
            conversation=conv, sender=other_user, body="tombstoned"
        )
        services.delete_message(message=doomed, actor=other_user)
        services.post_system_message(
            conversation_id=str(conv.id), body="video.call.ended:188"
        )

        assert _row(auth_client, conv)["last_message"]["body_preview"] is None
        assert _ids(auth_client.get(LIST, {"search": "tombstoned"})) == []
        assert _ids(auth_client.get(LIST, {"search": "video.call.ended"})) == []

    def test_a_label_a_row_draws_is_a_label_the_search_finds(
        self, auth_client, user, other_user, settings
    ):
        """The other side of the same rule: give a marker words and it becomes
        BOTH drawable and findable in one move. A deployment cannot end up with
        a row whose visible last line the search box cannot see."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        quiet = services.create_direct(owner=user, other_user_id=_user("dana").id)
        services.post_message(conversation=quiet, sender=other_user, body="unrelated")
        services.post_system_message(
            conversation_id=str(conv.id), body="video.call.ended:188"
        )

        assert _ids(auth_client.get(LIST, {"search": "call ended"})) == []
        settings.STAPEL_CHAT = {
            "SYSTEM_LINE_LABELS": {"video.call.ended": "Call ended"}
        }
        assert _row(auth_client, conv)["last_message"]["body_preview"] == "Call ended"
        assert _ids(auth_client.get(LIST, {"search": "call ended"})) == [str(conv.id)]
        assert _ids(auth_client.get(LIST, {"search": "CALL"})) == [str(conv.id)]

    def test_the_rule_itself(self):
        """The unit the two halves share, including the case no message shape
        above can produce over HTTP: a body-less (attachment-only) message
        draws no line either."""
        assert services.drawn_last_line(kind="text", body="hi", deleted=False) == "hi"
        assert services.drawn_last_line(kind="text", body="hi", deleted=True) is None
        assert services.drawn_last_line(kind="text", body="", deleted=False) is None
        assert services.drawn_last_line(kind="text", body="  ", deleted=False) is None
        assert (
            services.drawn_last_line(kind="system", body="x.y", deleted=False) is None
        )
