"""The attachment contract: an open registry, and metadata that renders.

Two properties are under test, and they are the two the owner named.

**Open.** Stickers are the next attachment type and they are not in this
release. If adding one required editing this package, the contract would have
to be reopened for every future kind — so a host registers a type through the
same merge-over-builtins seam every other Stapel registry uses, and the send
path accepts it immediately.

**Complete on first frame.** An attachment carries aspect, byte size and a
base64 micro-thumbnail (images), duration and a waveform image (voice), mime
and extension (documents) — so a bubble paints without a second round trip and
without reflowing when the real asset lands. The numbers come from
``cdn.describe`` by comm; nothing here re-derives them.
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
            "image", "gif", "video", "voice", "file"
        }

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
        settings.STAPEL_CHAT = {"ATTACHMENT_TYPES": {"voice": None}}
        assert "voice" not in get_attachment_types()
        with pytest.raises(UnknownAttachmentType):
            normalize_attachment({"key": "audio/x", "type": "voice"})

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
        for field in ("mime", "bytes", "width", "height", "aspect",
                      "duration_ms", "preview_b64", "waveform_b64"):
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
    """`cdn.describe` is the metadata authority; chat consumes it by comm."""

    def _register_describe(self, snapshot):
        name = "cdn.describe"
        function_registry._providers.pop(name, None)

        @function(name)
        def _describe(payload):
            return dict(snapshot, ref=payload["ref"])

        return _describe

    def teardown_method(self):
        function_registry._providers.pop("cdn.describe", None)

    def test_an_image_gets_aspect_bytes_and_a_micro_thumbnail(self, settings):
        self._register_describe(
            {
                "mime": "image/webp",
                "bytes": 91234,
                "width": 1200,
                "height": 800,
                "aspect": 1.5,
                "duration_ms": None,
                "preview_b64": "data:image/webp;base64,AAAA",
                "square": False,
                "variants": [{"tier": 240, "branch": "w", "url": "/m/240w.webp"}],
            }
        )
        [out] = prepare_attachments([{"key": "product/abc", "type": "image"}])
        assert out["aspect"] == 1.5, "the number that reserves the box"
        assert out["bytes"] == 91234
        assert out["preview_b64"].startswith("data:image/webp;base64,")
        assert out["variants"], "srcset geometry for a responsive img"

    def test_a_voice_message_gets_duration_and_a_waveform(self):
        self._register_describe(
            {
                "mime": "audio/ogg",
                "bytes": 4096,
                "duration_ms": 7200,
                "waveform_b64": "data:image/webp;base64,BBBB",
            }
        )
        [out] = prepare_attachments([{"key": "audio/abc", "type": "voice"}])
        assert out["duration_ms"] == 7200
        assert out["waveform_b64"].startswith("data:image/")

    def test_a_video_gets_a_poster_and_a_duration(self):
        self._register_describe(
            {
                "mime": "video/mp4",
                "bytes": 8_000_000,
                "width": 1920,
                "height": 1080,
                "aspect": 1.777,
                "duration_ms": 30_000,
                "preview_b64": "data:image/webp;base64,CCCC",
            }
        )
        [out] = prepare_attachments([{"key": "video/abc", "type": "video"}])
        assert out["duration_ms"] == 30_000
        assert out["preview_b64"]
        assert out["aspect"] == pytest.approx(1.777)

    def test_the_cdn_wins_over_what_the_client_claimed(self):
        self._register_describe({"bytes": 100, "aspect": 2.0})
        [out] = prepare_attachments(
            [{"key": "product/abc", "type": "image", "bytes": 999999, "aspect": 0.1}]
        )
        assert out["bytes"] == 100 and out["aspect"] == 2.0

    def test_an_unknown_field_from_a_newer_cdn_is_carried_through(self):
        """Forward compatibility: a field this release never heard of still
        reaches the client on the wire."""
        self._register_describe({"bytes": 1, "blurhash": "L6PZ"})
        [out] = prepare_attachments([{"key": "product/abc", "type": "image"}])
        assert out["blurhash"] == "L6PZ"

    def test_an_unreachable_cdn_does_not_fail_the_send(self, user, other_user):
        """The worst case is a bubble that renders from the sender's own
        numbers — never a message that refuses to send."""
        function_registry._providers.pop("cdn.describe", None)
        conv = services.create_direct(owner=user, other_user_id=other_user.id)
        msg = services.post_message(
            conversation=conv,
            sender=user,
            body="look",
            attachments=[{"key": "product/abc", "type": "image", "aspect": 1.5}],
        )
        assert msg.attachments[0]["aspect"] == 1.5

    def test_the_client_metadata_mode_makes_no_call(self, settings, user, other_user):
        settings.STAPEL_CHAT = {"ATTACHMENT_METADATA": "client"}
        self._register_describe({"bytes": 12345})
        [out] = prepare_attachments([{"key": "product/abc", "type": "image"}])
        assert out["bytes"] is None


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
