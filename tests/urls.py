"""Bare test urlconf — mounts chat at ``chat/`` (its own ``api/*`` underneath),
the historical single-module test layout."""
from importlib.util import find_spec

from django.urls import include, path

urlpatterns = [
    path("chat/", include("stapel_chat.urls")),
]

# The CDN's own routes, where it is installed for the seam test: a signed
# media link is resolved by reversing the CDN's URL and served by its view.
if find_spec("stapel_cdn"):
    urlpatterns.append(path("cdn/api/", include("stapel_cdn.urls")))
