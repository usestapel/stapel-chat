"""F1/F2 — the operator surface, which manufactured its own authority.

``SupportAssignView`` is ``IsAuthenticated`` and first-come-served. It calls
``services.assign_operator``, which writes a ``ConversationParticipant`` with
``role=OPERATOR``. Every later check on the thread asks ``_my_participant``
— and the row the caller just minted answers it. So one POST to one URL
turned any account into the operator of a stranger's support conversation,
with read, post and resolve following from the row it created (F1). The queue
that names those conversations was equally open, and it serializes
``scope_key`` and every participant's ``user_id`` (F2).

Neither is fixed by looking harder at the participant table: the row is real.
The missing question is the one before it — *may this caller act as an
operator in this scope at all* — and there was no seam that asked it. There
is now: ``ScopeProvider.can_operate``, answered by the shipped provider with
the mandate (a support operator is staff of some workspace; a customer opening
a ticket is not, and must stay able to open one).

Mutation-wise: delete the ``can_operate`` call from any of the three support
views and the matching test below fails.
"""
import pytest

from stapel_chat import services
from stapel_chat.models import ConversationParticipant
from stapel_core.comm import function_registry
from stapel_core.django.mandate import MANDATE_FUNCTION, MANDATE_RESULT_KEY

QUEUE = "/chat/api/v1/support/queue"


@pytest.fixture
def mandate_seam():
    """A deployment that CAN ask — the premise of the third state existing."""
    state = {"has_mandate": False, "raises": None}

    def handler(payload):
        if state["raises"]:
            raise state["raises"]
        return {MANDATE_RESULT_KEY: state["has_mandate"]}

    function_registry.register(MANDATE_FUNCTION, handler)
    yield state
    function_registry._providers.pop(MANDATE_FUNCTION, None)


@pytest.fixture(autouse=True)
def _clear_mandate_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def ticket(user):
    """A customer's support thread. The customer holds no mandate either —
    and must keep being able to open and use one."""
    return services.create_support(customer=user)


@pytest.fixture
def intruder_client(api_client, other_user):
    api_client.force_authenticate(user=other_user)
    return api_client


# ---------------------------------------------------------------------------
# F1 — the write that manufactured its own mandate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_mandate_less_account_cannot_claim_a_stranger_s_ticket(
    mandate_seam, intruder_client, ticket
):
    r = intruder_client.post(f"/chat/api/v1/support/conversations/{ticket.id}/assign")
    assert r.status_code == 403, r.content


@pytest.mark.django_db
def test_the_refused_claim_leaves_no_participant_row_behind(
    mandate_seam, intruder_client, ticket, other_user
):
    """The row IS the escalation: everything downstream trusts it. A refusal
    that still writes it has fixed nothing."""
    intruder_client.post(f"/chat/api/v1/support/conversations/{ticket.id}/assign")
    assert not ConversationParticipant.objects.filter(
        conversation=ticket, user=other_user
    ).exists()
    ticket.refresh_from_db()
    assert ticket.assigned_operator_id is None


@pytest.mark.django_db
def test_the_downstream_reads_stay_shut(mandate_seam, intruder_client, ticket):
    """What the manufactured row bought: history, posting, and resolve."""
    intruder_client.post(f"/chat/api/v1/support/conversations/{ticket.id}/assign")
    assert intruder_client.get(
        f"/chat/api/v1/conversations/{ticket.id}/messages"
    ).status_code == 403
    assert intruder_client.post(
        f"/chat/api/v1/conversations/{ticket.id}/messages",
        {"body": "hello"},
        format="json",
    ).status_code == 403
    assert intruder_client.post(
        f"/chat/api/v1/support/conversations/{ticket.id}/resolve"
    ).status_code == 403


# ---------------------------------------------------------------------------
# F2 — the queue is readable
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_mandate_less_account_cannot_read_the_operator_queue(
    mandate_seam, intruder_client, ticket
):
    """The rows carry scope_key and every participant's user_id."""
    r = intruder_client.get(QUEUE)
    assert r.status_code == 403, r.content


# ---------------------------------------------------------------------------
# The customer half must not become collateral damage
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_mandate_less_customer_still_opens_and_uses_a_ticket(
    mandate_seam, api_client, user
):
    """A support customer holding no workspace mandate is the normal case,
    not the attack. Closing the operator seam must not close the product."""
    api_client.force_authenticate(user=user)
    r = api_client.post(
        "/chat/api/v1/conversations", {"kind": "support"}, format="json"
    )
    assert r.status_code == 201, r.content
    conv_id = r.json()["id"]
    r = api_client.post(
        f"/chat/api/v1/conversations/{conv_id}/messages",
        {"body": "my order is late"},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert api_client.get(
        f"/chat/api/v1/conversations/{conv_id}/messages"
    ).status_code == 200


# ---------------------------------------------------------------------------
# The three states stay three
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_mandated_operator_still_works(mandate_seam, api_client, operator_user, ticket):
    mandate_seam["has_mandate"] = True
    api_client.force_authenticate(user=operator_user)
    assert api_client.get(QUEUE).status_code == 200
    r = api_client.post(f"/chat/api/v1/support/conversations/{ticket.id}/assign")
    assert r.status_code == 200, r.content
    assert api_client.post(
        f"/chat/api/v1/support/conversations/{ticket.id}/resolve"
    ).status_code == 200


@pytest.mark.django_db
def test_could_not_ask_refuses_with_503_never_403(mandate_seam, intruder_client, ticket):
    mandate_seam["raises"] = RuntimeError("workspaces is down")
    assert intruder_client.get(QUEUE).status_code == 503
    r = intruder_client.post(f"/chat/api/v1/support/conversations/{ticket.id}/assign")
    assert r.status_code == 503, r.content


@pytest.mark.django_db
def test_a_standalone_deployment_keeps_its_operator_surface(
    api_client, operator_user, ticket
):
    """No seam wired at all: nothing holds a mandate, so refusing every
    operator would be a different bug. The system check warns instead."""
    api_client.force_authenticate(user=operator_user)
    assert api_client.get(QUEUE).status_code == 200
    assert api_client.post(
        f"/chat/api/v1/support/conversations/{ticket.id}/assign"
    ).status_code == 200
