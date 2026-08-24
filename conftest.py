def _optional_test_apps() -> tuple:
    from importlib.util import find_spec

    return ("stapel_moderation",) if find_spec("stapel_moderation") else ()


def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        # Single source of truth for this block lives in _codegen_settings.py so
        # the test harness and the contract-emission harness (make contract) can
        # never drift (contract-pipeline.md §3).
        from stapel_chat._codegen_settings import settings_kwargs

        # stapel-moderation is an OPTIONAL dependency of this module (see
        # moderation.py); the tests install it so the seam is proven against
        # the real queue rather than a stand-in for it.
        settings.configure(**settings_kwargs(extra_apps=_optional_test_apps()))
        import django
        django.setup()

        from stapel_core.comm.schemas import autoload_schemas
        autoload_schemas()


import pytest  # noqa: E402


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient
    return APIClient()


def _make_user(username, email):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        username=username, email=email, password="x"
    )


@pytest.fixture
def user(db):
    return _make_user("alice", "alice@example.com")


@pytest.fixture
def other_user(db):
    return _make_user("bob", "bob@example.com")


@pytest.fixture
def operator_user(db):
    return _make_user("olga", "olga@example.com")


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def captured_events():
    """Subscribe to chat emits (in-process) and collect the Event envelopes.
    Delivery is synchronous with OUTBOX disabled, so the list is populated by
    the time emit() returns."""
    from stapel_core.comm import action_registry, subscribe_action

    collected = []

    def _handler(event):
        collected.append(event)

    names = ["chat.message", "chat.support.assigned"]
    for name in names:
        subscribe_action(name, _handler)
    try:
        yield collected
    finally:
        for name in names:
            handlers = action_registry._subscribers.get(name, [])
            if _handler in handlers:
                handlers.remove(_handler)
