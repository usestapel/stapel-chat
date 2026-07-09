"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

stapel-chat's own ``urls.py`` already bakes ``api/`` into every path
(``api/conversations``, ``api/support/queue``, ...) and documents its expected
host mount::

    path("chat/", include("stapel_chat.urls"))

This harness urlconf reproduces exactly that mount, so drf-spectacular emits
``/chat/api/...`` paths — the same ``<mod>/api/`` shape every pair-backend uses.
Chat is validated standalone (no monolith slice yet; contract-pipeline.md §9
fallback path applies).
"""
from django.conf.urls import include
from django.urls import path

urlpatterns = [
    path("chat/", include("stapel_chat.urls")),
]
