"""Finding one conversation in an inbox — `?search=` and `?unread=true`.

The pane that prompted this filtered the pages it had already loaded, because
`GET /conversations` took `anchor`, `direction` and `limit` and nothing else:
a search box over an inbox of three hundred read twenty rows and reported
"nothing found". The rule these tests pin is that the server searches the SAME
three things the row draws — who it is with, what it is about, and the last
line — and that filtering happens BEFORE the anchor page is taken, so paging a
search means paging the search rather than paging the inbox and hoping.
"""
import pytest

from stapel_chat import services
from stapel_chat.models import Conversation
from stapel_chat.subjects import register_subject_type, reset_subject_types

pytestmark = pytest.mark.django_db

LIST = "/chat/api/v1/conversations"


@pytest.fixture(autouse=True)
def _clean_subject_registry():
    from stapel_core.comm import function_registry

    reset_subject_types()
    yield
    reset_subject_types()
    function_registry._providers.pop("classified.subject_cards", None)


def _user(username, first="", last=""):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="x",
        first_name=first, last_name=last,
    )


def _ids(response):
    return [row["id"] for row in response.json()["items"]]


@pytest.fixture
def cards():
    """A stand-in for whoever owns the subject — the batched card Function."""
    from stapel_core.comm import function

    titles = {}

    @function("classified.subject_cards")
    def _cards(payload):
        return {
            "cards": {k: {"title": titles.get(k, k)} for k in payload["keys"]}
        }

    return titles


# ── The three fields a row is findable by ────────────────────────────────


class TestSearchByEachField:
    def test_by_the_counterparts_display_name(self, auth_client, user):
        seller = _user("gardenshed", first="Marta", last="Iversen")
        stranger = _user("someoneelse", first="Piet", last="Ruis")
        wanted = services.create_direct(owner=user, other_user_id=seller.id)
        other = services.create_direct(owner=user, other_user_id=stranger.id)

        # username, first name and last name are all the display name is made
        # of out of the box — each of them finds the same one row.
        for needle in ("gardenshed", "marta", "IVERSEN", "Mar"):
            found = _ids(auth_client.get(LIST, {"search": needle}))
            assert found == [str(wanted.id)], needle
        assert str(other.id) not in _ids(auth_client.get(LIST, {"search": "marta"}))

    def test_my_own_name_is_not_what_a_row_is_titled_with(self, auth_client, user):
        """The row says who it is WITH. Searching your own name is not a way
        to select your whole inbox — nothing on any row says it."""
        other = _user("bobbi")
        services.create_direct(owner=user, other_user_id=other.id)
        assert _ids(auth_client.get(LIST, {"search": user.username})) == []

    def test_by_the_linked_subjects_title(self, auth_client, user, cards):
        register_subject_type("listing", {"card_function": "classified.subject_cards"})
        seller = _user("seller1")
        cards["l-1"] = "Blue racing bicycle"
        cards["l-2"] = "Oak dining table"
        bike = services.create_direct(
            owner=user, other_user_id=seller.id,
            subject_type="listing", subject_key="l-1",
        )
        services.create_direct(
            owner=user, other_user_id=seller.id,
            subject_type="listing", subject_key="l-2",
        )

        assert _ids(auth_client.get(LIST, {"search": "racing"})) == [str(bike.id)]
        # And the card the row renders is served from the same resolution the
        # search already paid for.
        row = auth_client.get(LIST, {"search": "racing"}).json()["items"][0]
        assert row["subject"]["card"] == {"title": "Blue racing bicycle"}

    def test_by_the_last_line(self, auth_client, user, other_user):
        talked = services.create_direct(owner=user, other_user_id=other_user.id)
        third = _user("carol")
        quiet = services.create_direct(owner=user, other_user_id=third.id)
        services.post_message(
            conversation=talked, sender=other_user, body="Is the bicycle still there?"
        )
        services.post_message(conversation=quiet, sender=third, body="hello")

        assert _ids(auth_client.get(LIST, {"search": "BICYCLE"})) == [str(talked.id)]

    def test_only_the_last_line_the_row_actually_draws(
        self, auth_client, user, other_user
    ):
        """An older message is not on the row, a tombstone renders as deleted
        and a system line is machine vocabulary — none of the three is text a
        reader can see, so none of them is findable."""
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(conversation=conv, sender=other_user, body="parasol")
        services.post_message(conversation=conv, sender=other_user, body="last word")
        assert _ids(auth_client.get(LIST, {"search": "parasol"})) == []

        doomed = services.post_message(
            conversation=conv, sender=other_user, body="tombstoned"
        )
        services.delete_message(message=doomed, actor=other_user)
        assert _ids(auth_client.get(LIST, {"search": "tombstoned"})) == []

        services.post_system_message(
            conversation_id=str(conv.id), body="video.call.ended:188"
        )
        assert _ids(auth_client.get(LIST, {"search": "video.call.ended"})) == []


# ── unread=true ──────────────────────────────────────────────────────────


class TestUnreadOnly:
    def test_only_conversations_with_a_badge(self, auth_client, user, other_user):
        unread = services.create_direct(owner=user, other_user_id=other_user.id)
        third = _user("dana")
        read = services.create_direct(owner=user, other_user_id=third.id)
        services.post_message(conversation=unread, sender=other_user, body="hi")
        msg = services.post_message(conversation=read, sender=third, body="hi")
        services.mark_read(conversation=read, user=user, upto_seq=msg.seq)

        rows = auth_client.get(LIST, {"unread": "true"}).json()["items"]
        assert [r["id"] for r in rows] == [str(unread.id)]
        assert rows[0]["unread_count"] == 1

    def test_the_filter_and_the_badge_are_one_rule(self, auth_client, user, other_user):
        """My own message raises no badge, and a message deleted before I got
        to it raises none either — the chip must agree with both."""
        mine = services.create_direct(owner=user, other_user_id=other_user.id)
        services.post_message(conversation=mine, sender=user, body="mine")
        third = _user("erik")
        withdrawn = services.create_direct(owner=user, other_user_id=third.id)
        gone = services.post_message(conversation=withdrawn, sender=third, body="oops")
        services.delete_message(message=gone, actor=third)

        assert _ids(auth_client.get(LIST, {"unread": "true"})) == []
        # …and every row still reports the count the filter agreed with.
        assert all(
            row["unread_count"] == 0
            for row in auth_client.get(LIST).json()["items"]
        )

    def test_the_annotated_count_equals_the_per_row_one(
        self, auth_client, user, other_user
    ):
        """The page annotates the count; a single conversation still counts it
        per row. Two implementations of one number is two chances to be wrong,
        so they are compared over a deliberately mixed inbox."""
        quiet = services.create_direct(owner=user, other_user_id=other_user.id)
        third = _user("frida")
        busy = services.create_direct(owner=user, other_user_id=third.id)
        services.post_message(conversation=quiet, sender=user, body="mine only")
        for i in range(3):
            services.post_message(conversation=busy, sender=third, body=f"m{i}")
        seen = services.post_message(conversation=busy, sender=third, body="read me")
        services.mark_read(conversation=busy, user=user, upto_seq=1)
        assert seen.seq  # the marker sits below it on purpose

        by_row = {
            str(c.id): services.unread_count(
                conversation=c,
                participant=c.participants.get(user=user),
            )
            for c in (quiet, busy)
        }
        annotated = {
            row["id"]: row["unread_count"]
            for row in auth_client.get(LIST).json()["items"]
        }
        assert annotated == by_row
        assert by_row[str(busy.id)] == 3

    def test_anything_but_a_yes_is_no_filter(self, auth_client, user, other_user):
        services.create_direct(owner=user, other_user_id=other_user.id)
        for value in ("false", "0", "", "perhaps"):
            assert len(_ids(auth_client.get(LIST, {"unread": value}))) == 1


# ── Composition: the two filters, and the anchor ─────────────────────────


class TestComposition:
    def test_search_and_unread_together(self, auth_client, user):
        seller = _user("marta")
        buyer = _user("martin")
        read_thread = services.create_direct(owner=user, other_user_id=seller.id)
        unread_thread = services.create_direct(owner=user, other_user_id=buyer.id)
        seen = services.post_message(
            conversation=read_thread, sender=seller, body="one"
        )
        services.mark_read(conversation=read_thread, user=user, upto_seq=seen.seq)
        services.post_message(conversation=unread_thread, sender=buyer, body="two")

        # "mart" alone finds both; with the chip on, only the one with a badge.
        assert len(_ids(auth_client.get(LIST, {"search": "mart"}))) == 2
        assert _ids(auth_client.get(LIST, {"search": "mart", "unread": "true"})) == [
            str(unread_thread.id)
        ]

    def test_paging_walks_the_FILTERED_list(self, auth_client, user):
        """Filter first, then page. The anchor keeps its meaning: it is a
        position in the result the caller is looking at, and walking it must
        never surface a row the filter excluded."""
        wanted, ignored = [], []
        for i in range(4):
            wanted.append(
                services.create_direct(
                    owner=user, other_user_id=_user(f"marta{i}").id
                )
            )
            ignored.append(
                services.create_direct(
                    owner=user, other_user_id=_user(f"stranger{i}").id
                )
            )

        seen, anchor, pages = [], None, 0
        while True:
            params = {"search": "marta", "limit": 2}
            if anchor:
                params["anchor"] = anchor
            body = auth_client.get(LIST, params).json()
            seen.extend(row["id"] for row in body["items"])
            pages += 1
            if not body["has_next"] or pages > 5:
                break
            anchor = body["next_anchor"]

        assert pages == 2
        assert sorted(seen) == sorted(str(c.id) for c in wanted)
        assert not set(seen) & {str(c.id) for c in ignored}
        assert len(seen) == len(set(seen))  # no row served twice

    def test_a_backward_page_stays_inside_the_filter(self, auth_client, user):
        for i in range(3):
            services.create_direct(owner=user, other_user_id=_user(f"marta{i}").id)
            services.create_direct(owner=user, other_user_id=_user(f"other{i}").id)

        first = auth_client.get(LIST, {"search": "marta", "limit": 2}).json()
        back = auth_client.get(
            LIST,
            {"search": "marta", "limit": 2, "anchor": first["next_anchor"],
             "direction": "prev"},
        ).json()
        assert back["items"]
        for row in back["items"]:
            assert any(
                p["user_id"] != str(user.id) for p in row["participants"]
            )
        found = {row["id"] for row in back["items"]}
        assert found <= {
            str(c.id)
            for c in Conversation.objects.filter(
                participants__user__username__startswith="marta"
            )
        }


# ── The cost of all of it ────────────────────────────────────────────────


class TestQueryCount:
    """No per-row queries — on the plain list, on a search, or on the chip.

    The number of queries a page costs is measured at two sizes and must be the
    SAME number. An inbox that costs one more query per conversation is the
    shape that only shows up in production, where the inbox is not three rows.
    """

    @staticmethod
    def _measure(auth_client, user, params, rows, tag, previews=True):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(rows):
            other = _user(f"marta{tag}{i}")
            conv = services.create_direct(owner=user, other_user_id=other.id)
            # Sent by the OTHER side, so every row is unread for the viewer and
            # the same fixture measures the chip as well.
            services.post_message(
                conversation=conv, sender=other, body="about the marta thing"
            )
        with CaptureQueriesContext(connection) as ctx:
            response = auth_client.get(LIST, params)
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == rows
        if previews:
            # A page whose rows came back empty would hold the query count
            # flat by measuring nothing. Every row here draws its last line.
            assert all(
                row["last_message"]["body_preview"] == "about the marta thing"
                for row in items
            )
        return len(ctx.captured_queries)

    @pytest.mark.parametrize(
        "params",
        [{}, {"search": "marta"}, {"unread": "true"},
         {"search": "marta", "unread": "true"}],
        ids=["plain", "search", "unread", "search+unread"],
    )
    def test_the_page_costs_the_same_at_two_and_at_six(
        self, auth_client, user, params
    ):
        small = self._measure(auth_client, user, params, 2, "a")
        Conversation.objects.all().delete()
        large = self._measure(auth_client, user, params, 6, "b")
        assert small == large

    def test_the_last_message_projection_costs_no_query_at_all(
        self, auth_client, user, monkeypatch
    ):
        """The 0.8.3 projection rides in the SELECT the list already runs.

        Measured against the page as it was WITHOUT it — annotation off and the
        DTO field off, which is the 0.8.2 shape — because "the count did not
        grow with the row count" would also hold for a projection that cost one
        extra query flat, and one extra query per page is still a client's
        first request for a preview it should have been handed.
        """
        import stapel_chat.views as views

        monkeypatch.setattr(views.services, "with_last_message", lambda qs: qs)
        monkeypatch.setattr(views, "last_message_to_dto", lambda conv: None)
        before = self._measure(auth_client, user, {}, 4, "c", previews=False)

        monkeypatch.undo()
        Conversation.objects.all().delete()
        after = self._measure(auth_client, user, {}, 4, "d")
        assert after == before

    def test_one_card_call_for_a_whole_search(self, auth_client, user):
        """The search resolves cards to match titles; the page then renders
        them. That is ONE round trip to the catalogue, not two."""
        from stapel_core.comm import function

        calls = []

        @function("classified.subject_cards")
        def _counting(payload):
            calls.append(sorted(payload["keys"]))
            return {"cards": {k: {"title": f"Bicycle {k}"} for k in payload["keys"]}}

        register_subject_type("listing", {"card_function": "classified.subject_cards"})
        seller = _user("seller9")
        for i in range(4):
            services.create_direct(
                owner=user, other_user_id=seller.id,
                subject_type="listing", subject_key=f"l-{i}",
            )

        assert len(_ids(auth_client.get(LIST, {"search": "bicycle"}))) == 4
        assert len(calls) == 1


# ── The two knobs ────────────────────────────────────────────────────────


class TestConfiguration:
    def test_the_name_fields_are_the_deployments_to_choose(
        self, auth_client, user, settings
    ):
        other = _user("hidden", first="Findable")
        services.create_direct(owner=user, other_user_id=other.id)
        settings.STAPEL_CHAT = {"SEARCH_NAME_FIELDS": ["first_name"]}
        assert len(_ids(auth_client.get(LIST, {"search": "Findable"}))) == 1
        assert _ids(auth_client.get(LIST, {"search": "hidden"})) == []

    def test_a_field_path_that_does_not_resolve_is_a_boot_error(self, settings):
        from stapel_chat.checks import check_search_name_fields

        settings.STAPEL_CHAT = {"SEARCH_NAME_FIELDS": ["nickname"]}
        issues = check_search_name_fields(None)
        assert [i.id for i in issues] == ["stapel_chat.E021"]
        settings.STAPEL_CHAT = {"SEARCH_NAME_FIELDS": ["username"]}
        assert check_search_name_fields(None) == []

    def test_the_search_fields_of_a_card_are_the_subject_types_to_name(
        self, auth_client, user, settings
    ):
        from stapel_core.comm import function

        @function("classified.subject_cards")
        def _cards(payload):
            return {"cards": {k: {"headline": "Blue bicycle"} for k in payload["keys"]}}

        settings.STAPEL_CHAT = {
            "SUBJECT_TYPES": {
                "listing": {
                    "card_function": "classified.subject_cards",
                    "search_fields": ["headline"],
                }
            }
        }
        seller = _user("seller8")
        conv = services.create_direct(
            owner=user, other_user_id=seller.id,
            subject_type="listing", subject_key="l-9",
        )
        assert _ids(auth_client.get(LIST, {"search": "bicycle"})) == [str(conv.id)]

    def test_a_zero_scan_asks_no_catalogue_anything(
        self, auth_client, user, settings, cards
    ):
        register_subject_type("listing", {"card_function": "classified.subject_cards"})
        seller = _user("seller7")
        cards["l-7"] = "Blue bicycle"
        services.create_direct(
            owner=user, other_user_id=seller.id,
            subject_type="listing", subject_key="l-7",
        )
        settings.STAPEL_CHAT = {"SEARCH_SUBJECT_SCAN": 0}
        assert _ids(auth_client.get(LIST, {"search": "bicycle"})) == []
        # The other two fields are untouched by the knob.
        assert len(_ids(auth_client.get(LIST, {"search": "seller7"}))) == 1

    def test_the_scan_bound_is_the_newest_threads_and_it_says_so(
        self, auth_client, user, settings, cards, caplog
    ):
        register_subject_type("listing", {"card_function": "classified.subject_cards"})
        seller = _user("seller6")
        made = []
        for i in range(3):
            cards[f"l-{i}"] = "Blue bicycle"
            made.append(
                services.create_direct(
                    owner=user, other_user_id=seller.id,
                    subject_type="listing", subject_key=f"l-{i}",
                )
            )
        settings.STAPEL_CHAT = {"SEARCH_SUBJECT_SCAN": 1}
        with caplog.at_level("WARNING", logger="stapel_chat.services"):
            found = _ids(auth_client.get(LIST, {"search": "bicycle"}))
        # The newest subject thread is the one that was scanned…
        assert found == [str(made[-1].id)]
        # …and the truncation is announced rather than looking like an empty
        # catalogue.
        assert any("scanned only the newest" in r.message for r in caplog.records)
