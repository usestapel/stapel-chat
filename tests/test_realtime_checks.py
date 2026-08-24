"""The checks that make a polling chat impossible to ship by accident.

This is the 0.3.0 mechanism under test. A product opened its chat and found
Enter did not send and the panel said "updates every few seconds" — a chat that
had a WebSocket implementation, a mounted route, a channel layer and a proxy in
front of it, and still polled, because the browser's handshake could not carry
the credential the deployment authenticates with. Nothing anywhere said so; the
fallback was silent by design.

Every check below converts one silent route to that outcome into a boot
failure. There is deliberately no setting that turns them off — an opt-out is
how the next deployment would arrive at the same place.
"""
from django.test import override_settings

from stapel_chat.checks import (
    check_activity_registry,
    check_attachment_registry,
    check_channel_layer,
    check_origin_guard,
    check_realtime_substrate,
    check_signal_transport,
    check_websocket_credential_channel,
)

COOKIE_STACK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "stapel_core.django.jwt.authentication.JWTCookieAuthentication"
    ]
}
HEADER_STACK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication"
    ]
}


def _ids(messages):
    return [m.id for m in messages]


class TestTheHarnessIsClean:
    """The test settings are what a correct host looks like."""

    def test_no_realtime_check_fires_by_default(self):
        assert check_realtime_substrate(None) == []
        assert check_channel_layer(None) == []
        assert check_signal_transport(None) == []
        assert check_attachment_registry(None) == []
        assert check_activity_registry(None) == []


class TestSubstrateAndTransport:
    def test_missing_substrate_app_is_an_error(self):
        installed = [
            app
            for app in __import__("django.conf", fromlist=["settings"]).settings.INSTALLED_APPS
            if app != "stapel_realtime"
        ]
        with override_settings(INSTALLED_APPS=installed):
            assert _ids(check_realtime_substrate(None)) == ["stapel_chat.E010"]

    def test_no_channel_layer_is_an_error_not_a_warning(self):
        """A socket with no layer connects, replays once and goes silent while
        every new message reaches only whoever is in the same process. That is
        the configuration that makes a product poll."""
        with override_settings(CHANNEL_LAYERS={}):
            issues = check_channel_layer(None)
        assert _ids(issues) == ["stapel_chat.E011"]
        assert issues[0].level >= 40  # ERROR, so `manage.py check` refuses

    def test_a_layer_without_a_default_backend_is_still_an_error(self):
        with override_settings(CHANNEL_LAYERS={"other": {"BACKEND": "x"}}):
            assert _ids(check_channel_layer(None)) == ["stapel_chat.E011"]

    def test_no_signal_transport_is_an_error(self):
        """The journal would still work — which is exactly why this is worth
        failing on. The result looks like a working chat with dead ticks and a
        conversation list that only moves when you reload it."""
        with override_settings(
            STAPEL_COMM={
                "OUTBOX_ENABLED": False,
                "ACTION_TRANSPORT": "inprocess",
                "VALIDATE_SCHEMAS": True,
                "SIGNAL_TRANSPORT": "none",
            }
        ):
            assert _ids(check_signal_transport(None)) == ["stapel_chat.E013"]


class TestTheCredentialChannel:
    """The actual root cause of the polling product.

    A browser cannot set an Authorization header on ``new WebSocket()``. Where
    the HTTP stack authenticates by cookie and the Channels middleware has no
    cookie branch, every handshake from a browser closes 4401 — and a client
    that reads 4401 as a permanent refusal stops retrying and falls back to a
    timer.
    """

    def test_a_cookie_host_whose_ws_middleware_cannot_read_cookies_is_an_error(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "stapel_chat.checks._ws_middleware_reads_cookies", lambda name: False
        )
        with override_settings(REST_FRAMEWORK=COOKIE_STACK, JWT_COOKIE_NAME="stapel_jwt"):
            issues = check_websocket_credential_channel(None)
        assert _ids(issues) == ["stapel_chat.E012"]
        assert "4401" in issues[0].msg
        assert "polling" in issues[0].msg

    def test_a_bearer_host_is_not_affected(self, monkeypatch):
        monkeypatch.setattr(
            "stapel_chat.checks._ws_middleware_reads_cookies", lambda name: False
        )
        with override_settings(REST_FRAMEWORK=HEADER_STACK):
            assert check_websocket_credential_channel(None) == []

    def test_a_cookie_host_whose_middleware_does_read_cookies_is_fine(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "stapel_chat.checks._ws_middleware_reads_cookies", lambda name: True
        )
        with override_settings(REST_FRAMEWORK=COOKIE_STACK):
            assert check_websocket_credential_channel(None) == []

    def test_the_probe_asks_the_middleware_rather_than_pinning_a_version(self):
        """A behavioural question deserves a behavioural answer, so the check
        hands the extractor a handshake carrying only a cookie and reads the
        verdict off the result."""
        from stapel_chat.checks import _ws_middleware_reads_cookies

        assert _ws_middleware_reads_cookies("stapel_jwt") in (True, False)

    def test_a_probe_failure_never_asserts(self, monkeypatch):
        """A check that cannot ask must not claim an answer."""
        import stapel_core.django.jwt.channels as ch

        def _boom(scope):
            raise RuntimeError("nope")

        monkeypatch.setattr(ch, "_extract_token", _boom)
        from stapel_chat.checks import _ws_middleware_reads_cookies

        assert _ws_middleware_reads_cookies("stapel_jwt") is True


class TestOriginGuard:
    def test_cookie_auth_without_an_origin_allowlist_is_an_error(self):
        """A cookie is ambient authority: the browser attaches it to a
        handshake started by any page on the internet. stapel-realtime warns
        about an empty allowlist; where the socket authenticates by cookie it
        is cross-site WebSocket hijacking of a live conversation."""
        with override_settings(
            REST_FRAMEWORK=COOKIE_STACK, STAPEL_REALTIME={"ALLOWED_ORIGINS": []}
        ):
            assert _ids(check_origin_guard(None)) == ["stapel_chat.E014"]

    def test_a_populated_allowlist_clears_it(self):
        with override_settings(
            REST_FRAMEWORK=COOKIE_STACK,
            STAPEL_REALTIME={"ALLOWED_ORIGINS": ["https://app.example.com"]},
        ):
            assert check_origin_guard(None) == []

    def test_a_bearer_host_is_not_asked(self):
        with override_settings(
            REST_FRAMEWORK=HEADER_STACK, STAPEL_REALTIME={"ALLOWED_ORIGINS": []}
        ):
            assert check_origin_guard(None) == []


class TestRegistryChecks:
    def test_a_non_dict_attachment_spec_is_caught_at_boot(self):
        with override_settings(STAPEL_CHAT={"ATTACHMENT_TYPES": {"sticker": "yes"}}):
            assert _ids(check_attachment_registry(None)) == ["stapel_chat.E015"]

    def test_a_non_sequence_fields_entry_is_caught(self):
        with override_settings(
            STAPEL_CHAT={"ATTACHMENT_TYPES": {"sticker": {"fields": 3}}}
        ):
            assert _ids(check_attachment_registry(None)) == ["stapel_chat.E015"]

    def test_removing_a_builtin_with_none_is_legal(self):
        with override_settings(STAPEL_CHAT={"ATTACHMENT_TYPES": {"voice": None}}):
            assert check_attachment_registry(None) == []

    def test_a_negative_activity_ttl_is_caught_at_boot(self):
        with override_settings(
            STAPEL_CHAT={"ACTIVITY_STATES": {"pondering": {"ttl_s": -1}}}
        ):
            assert _ids(check_activity_registry(None)) == ["stapel_chat.E016"]

    def test_a_non_dict_activity_spec_is_caught(self):
        with override_settings(STAPEL_CHAT={"ACTIVITY_STATES": {"pondering": 5}}):
            assert _ids(check_activity_registry(None)) == ["stapel_chat.E016"]


class TestNoOptOut:
    def test_there_is_no_setting_that_disables_realtime(self):
        """The knob would be the defect. The product that shipped "updates
        every few seconds" got there without anyone choosing it, and an opt-out
        is how the next one would too."""
        from stapel_chat.conf import DEFAULTS

        assert not [
            key
            for key in DEFAULTS
            if key in {"REALTIME", "REALTIME_ENABLED", "POLLING", "WEBSOCKETS"}
        ]
