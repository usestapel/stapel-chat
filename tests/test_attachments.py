"""The attachment contract: an open registry, and metadata that renders.

Two properties are under test, and they are the two the owner named.

**Open.** Stickers are the next attachment type and they are not in this
release. If adding one required editing this package, the contract would have
to be reopened for every future kind — so a host registers a type through the
same merge-over-builtins seam every other Stapel registry uses, and the send
path accepts it immediately.

**Complete on first frame.** An attachment carries aspect, byte size and a
base64 micro-thumbnail (images), duration and a waveform preview (voice), mime
and extension (documents) — so a bubble paints without a second round trip and
without reflowing when the real asset lands. The numbers come from stapel-cdn's
``cdn.describe_many`` by comm, one call per message; nothing here re-derives
them, and the two packages share one vocabulary rather than translating between
two.
"""
import pytest
from stapel_core.comm import function, function_registry

from stapel_chat import services
from stapel_chat.attachments import (
    BUILTIN_ATTACHMENT_TYPES,
    InvalidAttachment,
    UnknownAttachmentType,
    attachment_type_names,
    get_attachment_types,
    normalize_attachment,
    prepare_attachments,
    register_attachment_type,
    reset_attachment_types,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_attachment_types()
    yield
    reset_attachment_types()


class TestOpenRegistry:
    def test_the_builtins_cover_every_kind_the_owner_named(self):
        assert set(BUILTIN_ATTACHMENT_TYPES) == {
            "image", "gif", "video", "audio", "file"
        }

    def test_the_type_names_ARE_the_cdn_kind_names(self):
        """One vocabulary, asserted rather than agreed by comment.

        chat's `type` and stapel-cdn's media `kind` name the same thing. They
        were briefly allowed to drift (chat said `voice` where the CDN said
        `audio`) and that is exactly the seam defect this fleet keeps paying
        for. If the two ever diverge again, this fails.
        """
        try:
            from stapel_cdn.kinds import BUILTIN_MEDIA_KINDS
        except ImportError:
            pytest.skip("stapel-cdn is not installed in this environment")
        assert set(BUILTIN_ATTACHMENT_TYPES) == set(BUILTIN_MEDIA_KINDS)

    def test_preview_kind_matches_the_cdn_for_every_builtin(self):
        """And so does what each type's preview DEPICTS — a client that
        reserves a waveform box for audio must be reserving it for the same
        reason the CDN renders one."""
        try:
            from stapel_cdn.kinds import BUILTIN_MEDIA_KINDS
        except ImportError:
            pytest.skip("stapel-cdn is not installed in this environment")
        ours = {n: (s or {}).get("preview_kind") for n, s in BUILTIN_ATTACHMENT_TYPES.items()}
        theirs = {n: (s or {}).get("preview") for n, s in BUILTIN_MEDIA_KINDS.items()}
        assert ours == theirs

    def test_a_host_registers_a_sticker_without_touching_this_package(self):
        register_attachment_type(
            "sticker", {"fields": ("mime", "bytes", "width", "height", "pack_id")}
        )
        assert "sticker" in attachment_type_names()
        out = normalize_attachment({"key": "sticker/abc", "type": "sticker"})
        assert out["type"] == "sticker"

    def test_settings_merge_over_builtins(self, settings):
        settings.STAPEL_CHAT = {"ATTACHMENT_TYPES": {"sticker": {"fields": ()}}}
        assert "sticker" in get_attachment_types()
        assert "image" in get_attachment_types(), "builtins survive a merge"

    def test_none_removes_a_builtin(self, settings):
        settings.STAPEL_CHAT = {"ATTACHMENT_TYPES": {"audio": None}}
        assert "audio" not in get_attachment_types()
        with pytest.raises(UnknownAttachmentType):
            normalize_attachment({"key": "audio/x", "type": "audio"})

    def test_runtime_registration_wins_over_settings(self, settings):
        settings.STAPEL_CHAT = {"ATTACHMENT_TYPES": {"sticker": {"fields": ("a",)}}}
        register_attachment_type("sticker", None)
        assert "sticker" not in get_attachment_types()

    def test_an_unregistered_type_is_refused_not_passed_through(self):
        """An open registry is not a free-for-all: the point of registering a
        type is that every subscriber knows what it may be asked to render."""
        with pytest.raises(UnknownAttachmentType):
            normalize_attachment({"key": "x/y", "type": "hologram"})


class TestNormalization:
    def test_every_base_field_is_present_even_when_unknown(self):
        out = normalize_attachment({"key": "file/x", "type": "file"})
        for field in ("mime", "bytes", "width", "height", "aspect", "square",
                      "animated", "duration_ms", "preview_b64", "preview_kind",
                      "poster_url", "meta_status", "meta_reason"):
            assert field in out, f"{field} must be present so a client never tests for it"

    def test_a_bare_ref_string_is_the_pre_0_3_form(self):
        out = normalize_attachment("chat/abc")
        assert out == {**out, "key": "chat/abc", "type": "file"}

    def test_an_attachment_without_a_key_is_refused(self):
        with pytest.raises(InvalidAttachment):
            normalize_attachment({"type": "image"})

    def test_extension_is_derived_from_the_name(self):
        out = normalize_attachment(
            {"key": "file/x", "type": "file", "name": "Q3 Report.PDF"}
        )
        assert out["ext"] == "pdf"

    def test_extension_falls_back_to_the_mime_type(self):
        out = normalize_attachment(
            {"key": "file/x", "type": "file", "mime": "application/pdf"}
        )
        assert out["ext"] == "pdf"

    def test_preview_kind_comes_from_the_registry_not_the_client(self):
        """It follows from the type, so it is known before any preview exists —
        which is what lets a client reserve the right-shaped placeholder while
        the CDN is still rendering. A client does not get to assert it."""
        out = normalize_attachment(
            {"key": "audio/x", "type": "audio", "preview_kind": "blur"}
        )
        assert out["preview_kind"] == "waveform"
        assert normalize_attachment({"key": "f/x", "type": "file"})["preview_kind"] is None

    def test_an_undescribed_attachment_says_so_rather_than_going_quiet(self):
        """No consumer ever meets an unexplained null."""
        out = normalize_attachment({"key": "image/x", "type": "image"})
        assert out["meta_status"] == "missing"
        assert out["meta_reason"] == "not_described"

    def test_an_oversized_preview_is_refused(self, settings):
        """A base64 preview is untrusted bytes riding inside every message
        frame, on their way to other people's screens."""
        settings.STAPEL_CHAT = {"MAX_PREVIEW_B64_BYTES": 64}
        with pytest.raises(InvalidAttachment):
            normalize_attachment(
                {
                    "key": "image/x",
                    "type": "image",
                    "preview_b64": "data:image/webp;base64," + "A" * 500,
                }
            )

    def test_a_preview_that_is_not_an_image_data_uri_is_refused(self):
        with pytest.raises(InvalidAttachment):
            normalize_attachment(
                {"key": "image/x", "type": "image", "preview_b64": "javascript:alert(1)"}
            )

    def test_too_many_attachments_are_refused(self, settings):
        settings.STAPEL_CHAT = {"MAX_ATTACHMENTS": 2}
        with pytest.raises(InvalidAttachment):
            prepare_attachments(["a/1", "a/2", "a/3"])


class TestCdnContract:
    """`cdn.describe_many` is the metadata authority; chat consumes it by comm.

    One call per message, not one per attachment — the per-ref round trip was
    the only thing left making that N.
    """

    def _register(self, per_ref: dict, missing=()):
        function_registry._providers.pop("cdn.describe_many", None)
        self.calls = []

        @function("cdn.describe_many")
        def _describe_many(payload):
            refs = list(payload["refs"])
            self.calls.append(refs)
            return {
                "items": {r: dict(per_ref, ref=r) for r in refs if r not in missing},
                "missing": [r for r in refs if r in missing],
            }

        return _describe_many

    def teardown_method(self):
        function_registry._providers.pop("cdn.describe_many", None)

    def test_an_image_gets_aspect_bytes_and_a_micro_thumbnail(self):
        self._register({
            "kind": "image", "mime": "image/webp", "ext": "webp", "bytes": 91234,
            "width": 1200, "height": 800, "aspect": 1.5, "square": False,
            "animated": False, "duration_ms": None,
            "preview_b64": "data:image/webp;base64,AAAA", "preview_kind": "blur",
            "poster_url": None, "meta_status": "ok", "meta_reason": None,
            "variants": [{"tier": 240, "branch": "w", "url": "/m/240w.webp"}],
        })
        [out] = prepare_attachments([{"key": "product/abc", "type": "image"}])
        assert out["aspect"] == 1.5, "the number that reserves the box"
        assert out["bytes"] == 91234
        assert out["preview_b64"].startswith("data:image/webp;base64,")
        assert out["preview_kind"] == "blur"
        assert out["meta_status"] == "ok"
        assert out["variants"], "srcset geometry for a responsive img"

    def test_a_voice_message_gets_duration_and_a_waveform_preview(self):
        self._register({
            "kind": "audio", "mime": "audio/ogg", "bytes": 4096,
            "duration_ms": 7200,
            "preview_b64": "data:image/webp;base64,BBBB",
            "preview_kind": "waveform", "meta_status": "ok",
        })
        [out] = prepare_attachments([{"key": "audio/abc", "type": "audio"}])
        assert out["duration_ms"] == 7200
        assert out["preview_kind"] == "waveform"
        assert out["preview_b64"].startswith("data:image/")

    def test_a_video_gets_a_poster_and_a_duration(self):
        self._register({
            "kind": "video", "mime": "video/mp4", "bytes": 8_000_000,
            "width": 1920, "height": 1080, "aspect": 1.777778,
            "duration_ms": 30_000, "preview_b64": "data:image/webp;base64,CCCC",
            "preview_kind": "poster", "poster_url": "/m/poster.webp",
            "meta_status": "ok",
        })
        [out] = prepare_attachments([{"key": "video/abc", "type": "video"}])
        assert out["duration_ms"] == 30_000
        assert out["preview_kind"] == "poster"
        assert out["poster_url"] == "/m/poster.webp"
        assert out["aspect"] == pytest.approx(1.7778, rel=1e-3)

    def test_the_whole_message_costs_ONE_call(self):
        self._register({"bytes": 1, "meta_status": "ok"})
        prepare_attachments([
            {"key": f"product/{i}", "type": "image"} for i in range(9)
        ])
        assert len(self.calls) == 1
        assert len(self.calls[0]) == 9

    def test_a_batch_is_paged_at_the_cdn_limit(self, settings):
        """batch size IS response size — every snapshot may inline a preview."""
        from stapel_chat.attachments import DESCRIBE_BATCH_LIMIT

        assert DESCRIBE_BATCH_LIMIT == 50
        settings.STAPEL_CHAT = {"MAX_ATTACHMENTS": 120}
        self._register({"bytes": 1, "meta_status": "ok"})
        prepare_attachments([
            {"key": f"product/{i}", "type": "image"} for i in range(120)
        ])
        assert [len(c) for c in self.calls] == [50, 50, 20]

    def test_an_unknown_ref_is_data_not_an_exception(self):
        """A message with one dead attachment still renders the other nine."""
        self._register({"bytes": 5, "meta_status": "ok"}, missing={"product/gone"})
        live, dead = prepare_attachments([
            {"key": "product/here", "type": "image"},
            {"key": "product/gone", "type": "image"},
        ])
        assert live["meta_status"] == "ok" and live["bytes"] == 5
        assert dead["meta_status"] == "missing"
        assert dead["meta_reason"] == "unknown_ref"

    def test_a_named_degradation_reaches_the_client(self):
        """"still generating" and "this deployment has no ffmpeg" are different
        placeholders, so the reason travels instead of being swallowed."""
        self._register({
            "kind": "video", "duration_ms": None, "preview_b64": None,
            "preview_kind": "poster",
            "meta_status": "partial", "meta_reason": "ffmpeg_missing",
        })
        [out] = prepare_attachments([{"key": "video/abc", "type": "video"}])
        assert out["meta_status"] == "partial"
        assert out["meta_reason"] == "ffmpeg_missing"
        assert out["preview_kind"] == "poster", "the placeholder shape is still known"

    def test_an_unmeasured_duration_stays_null_and_never_becomes_zero(self):
        """A zero-length voice message and an unmeasured one are different
        facts. A sender's optimistic guess must not survive the authority
        saying it does not know."""
        self._register({
            "kind": "audio", "duration_ms": None,
            "meta_status": "partial", "meta_reason": "not_generated",
        })
        [out] = prepare_attachments(
            [{"key": "audio/abc", "type": "audio", "duration_ms": 5000}]
        )
        assert out["duration_ms"] is None

    def test_the_cdn_wins_over_what_the_client_claimed(self):
        self._register({"bytes": 100, "aspect": 2.0, "meta_status": "ok"})
        [out] = prepare_attachments(
            [{"key": "product/abc", "type": "image", "bytes": 999999, "aspect": 0.1}]
        )
        assert out["bytes"] == 100 and out["aspect"] == 2.0

    def test_an_unknown_field_from_a_newer_cdn_is_carried_through(self):
        """Forward compatibility: a field this release never heard of still
        reaches the client on the wire."""
        self._register({"bytes": 1, "blurhash": "L6PZ", "meta_status": "ok"})
        [out] = prepare_attachments([{"key": "product/abc", "type": "image"}])
        assert out["blurhash"] == "L6PZ"

    def test_an_unreachable_cdn_does_not_fail_the_send(self, user, other_user):
        """The worst case is a bubble that renders from the sender's own
        numbers — never a message that refuses to send."""
        function_registry._providers.pop("cdn.describe_many", None)
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = services.post_message(
            conversation=conv,
            sender=user,
            body="look",
            attachments=[{"key": "product/abc", "type": "image", "aspect": 1.5}],
        )
        assert msg.attachments[0]["aspect"] == 1.5

    def test_the_client_metadata_mode_makes_no_call(self, settings):
        settings.STAPEL_CHAT = {"ATTACHMENT_METADATA": "client"}
        self._register({"bytes": 12345, "meta_status": "ok"})
        [out] = prepare_attachments([{"key": "product/abc", "type": "image"}])
        assert out["bytes"] is None
        assert self.calls == []


class TestSendCarriesTheDescriptor:
    def test_a_stored_message_keeps_the_full_descriptor(self, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = services.post_message(
            conversation=conv,
            sender=user,
            body="",
            attachments=[
                {
                    "key": "product/abc",
                    "type": "image",
                    "aspect": 1.5,
                    "bytes": 42,
                    "preview_b64": "data:image/webp;base64,AAAA",
                }
            ],
        )
        stored = msg.attachments[0]
        assert stored["type"] == "image"
        assert stored["aspect"] == 1.5
        assert stored["preview_b64"].startswith("data:image/webp")

    def test_an_unknown_type_is_a_400_over_rest(self, auth_client, user, other_user):
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        r = auth_client.post(
            f"/chat/api/v1/conversations/{conv.id}/messages",
            {"body": "", "attachments": [{"key": "x/y", "type": "hologram"}]},
            format="json",
        )
        assert r.status_code == 400
        assert (
            r.json()["localizable_error"] == "error.400.chat_unknown_attachment_type"
        )
