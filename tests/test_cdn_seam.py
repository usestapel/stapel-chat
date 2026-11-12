"""The attachment seam against the real stapel-cdn: upload, sign, expire, serve.

No fake stands in front of the seam. A voice note is uploaded through the
CDN's own HTTP intake, sent in a chat message, and fetched through the URLs
chat hands out — the CDN's signer, its ``describe_many`` and its signed-media
view do the rest. ``SIGNED_MEDIA_TTL_SECONDS`` is shortened so "after the TTL"
is a few seconds: the URL a reader got earlier stops working, a fresh read
gets one that works, and a stranger gets none.

Run against both CDN behaviours: the signing one (default) and
``PRIVATE_MEDIA_URLS = "public"``, which hands out permanent URLs the way a
CDN release before signing did — chat detects nothing and resolves the same.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from stapel_chat import services

from .siblings import requires

requires_cdn = requires("stapel_cdn", "stapel-cdn")

pytestmark = pytest.mark.django_db

TTL = 2


def _cdn_signs_private_media() -> bool:
    """Whether the installed stapel-cdn hands out signed links for private
    objects (the release after 0.30.0) or permanent public URLs (up to it).
    Chat must work against both; the expiry half is asserted where it exists."""
    from stapel_cdn import protected

    return hasattr(protected, "stored_url")
OGG = b"OggS" + b"\x01" * 600


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _body(response):
    if hasattr(response, "streaming_content"):
        return b"".join(response.streaming_content)
    return response.content


@pytest.fixture
def real_cdn(settings, tmp_path):
    """The CDN's own describe provider registered (another test may have
    replaced it with a fake), media in a scratch root, a short TTL, and no
    snapshot cache in chat so every read reaches the CDN."""
    from stapel_core.comm import function, function_registry

    from stapel_cdn import functions as cdn_functions

    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL = "/media/"
    settings.STAPEL_CDN = {"SIGNED_MEDIA_TTL_SECONDS": TTL}
    settings.STAPEL_CHAT = {"ATTACHMENT_URL_CACHE_S": 0}
    function_registry._providers.pop("cdn.describe_many", None)
    function(
        "cdn.describe_many", schema=cdn_functions.DESCRIBE_MANY_SCHEMA
    )(cdn_functions.describe_many)
    yield settings


@pytest.fixture
def stranger(db):
    return get_user_model().objects.create_user(
        username="mallory", email="mallory@example.com", password="x"
    )


def _upload_voice(client) -> str:
    from django.core.files.uploadedfile import SimpleUploadedFile

    r = client.post(
        "/cdn/api/v1/upload/audio/",
        {"file": SimpleUploadedFile("voice.ogg", OGG, content_type="audio/ogg")},
        format="multipart",
    )
    assert r.status_code == 201, r.content
    ref = r.json()["audio"]["ref"]
    assert ref.startswith("audio/")
    return ref


def _voice_url(client, conv) -> str:
    r = client.get(f"/chat/api/v1/conversations/{conv.id}/messages")
    assert r.status_code == 200, r.content
    [message] = r.json()["items"]
    [attachment] = message["attachments"]
    assert attachment["key"].startswith("audio/")
    [original] = [v for v in attachment["variants"] if v["tier"] == "original"]
    return original["url"]


def _send_voice(user, other_user):
    ref = _upload_voice(_client(user))
    conv = services.create_direct(owner=user, other_user_id=other_user.id)
    r = _client(user).post(
        f"/chat/api/v1/conversations/{conv.id}/messages",
        {"body": "", "attachments": [{"key": ref, "type": "audio"}]},
        format="json",
    )
    assert r.status_code == 201, r.content
    return conv, r.json()


@requires_cdn
def test_a_voice_note_still_plays_after_the_ttl(real_cdn, user, other_user, stranger):
    conv, sent = _send_voice(user, other_user)
    reader = _client(other_user)

    early = _voice_url(reader, conv)
    if _cdn_signs_private_media():
        assert "/cdn/api/v1/media/signed/" in early
        assert _body(reader.get(early)) == OGG

        time.sleep(TTL + 1.5)
        assert reader.get(early).status_code != 200, "the signed link expired"

        fresh = _voice_url(reader, conv)
        assert fresh != early
        assert _body(reader.get(fresh)) == OGG
    else:
        # A CDN that hands out permanent URLs: the read still resolves one,
        # served by the web server rather than the app.
        assert early.startswith("/media/")

    href = sent["attachments"][0]["href"]
    redirect = reader.get(href)
    assert redirect.status_code == 302
    if _cdn_signs_private_media():
        assert _body(reader.get(redirect["Location"])) == OGG

    outsider = _client(stranger)
    assert outsider.get(f"/chat/api/v1/conversations/{conv.id}/messages").status_code == 403
    assert outsider.get(href).status_code == 403


@requires_cdn
def test_the_row_holds_no_url_of_the_cdn(real_cdn, user, other_user):
    from stapel_chat.models import Message

    conv, _ = _send_voice(user, other_user)
    [stored] = Message.objects.get(conversation=conv).attachments
    assert "/media/" not in repr(stored)
    assert stored["key"].startswith("audio/")


@requires_cdn
def test_a_cdn_that_hands_out_permanent_urls_resolves_the_same(
    real_cdn, user, other_user
):
    real_cdn.STAPEL_CDN = {
        "SIGNED_MEDIA_TTL_SECONDS": TTL,
        "PRIVATE_MEDIA_URLS": "public",
    }
    conv, sent = _send_voice(user, other_user)
    reader = _client(other_user)
    url = _voice_url(reader, conv)
    assert url.startswith("/media/private/")
    redirect = reader.get(sent["attachments"][0]["href"])
    assert redirect.status_code == 302 and redirect["Location"] == url
