"""Bare test urlconf — mounts chat at ``chat/`` (its own ``api/*`` underneath),
the historical single-module test layout."""
from django.urls import include, path

urlpatterns = [
    path("chat/", include("stapel_chat.urls")),
]
