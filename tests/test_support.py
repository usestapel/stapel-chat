"""Support lifecycle: queue → assign → resolve → reopen, plus authz + events."""
import pytest

from stapel_chat import services
from stapel_chat.models import (
    ConversationParticipant,
    ParticipantRole,
    SupportStatus,
)


def _support(customer):
    return services.create_support(customer=customer)


def _op_client(api_client, operator_user):
    api_client.force_authenticate(user=operator_user)
    return api_client


@pytest.mark.django_db
class TestSupportCycle:
    def test_queue_lists_unassigned(self, api_client, user, operator_user):
        conv = _support(user)
        client = _op_client(api_client, operator_user)
        r = client.get("/chat/api/support/queue")
        assert r.status_code == 200
        ids = [c["id"] for c in r.json()["items"]]
        assert str(conv.id) in ids

    def test_assign_claims_and_leaves_queue(
        self, api_client, user, operator_user, captured_events
    ):
        conv = _support(user)
        client = _op_client(api_client, operator_user)
        r = client.post(f"/chat/api/support/conversations/{conv.id}/assign")
        assert r.status_code == 200
        assert r.json()["assigned_operator_id"] == str(operator_user.id)
        # operator is now an operator-role participant
        p = ConversationParticipant.objects.get(conversation=conv, user=operator_user)
        assert p.role == ParticipantRole.OPERATOR
        # emitted the assignment event
        assert any(
            e.event_type == "chat.support.assigned"
            and e.payload["operator_id"] == str(operator_user.id)
            for e in captured_events
        )
        # a system line marks the assignment in the thread
        assert conv.messages.filter(kind="system", body="chat.support.assigned").exists()
        # no longer in the queue
        r = client.get("/chat/api/support/queue")
        assert str(conv.id) not in [c["id"] for c in r.json()["items"]]

    def test_assign_is_idempotent_for_same_operator(self, api_client, user, operator_user):
        conv = _support(user)
        client = _op_client(api_client, operator_user)
        client.post(f"/chat/api/support/conversations/{conv.id}/assign")
        r = client.post(f"/chat/api/support/conversations/{conv.id}/assign")
        assert r.status_code == 200
        # only one assignment system line
        assert conv.messages.filter(body="chat.support.assigned").count() == 1

    def test_second_operator_gets_conflict(self, api_client, user, operator_user, other_user):
        conv = _support(user)
        _op_client(api_client, operator_user).post(
            f"/chat/api/support/conversations/{conv.id}/assign"
        )
        r = _op_client(api_client, other_user).post(
            f"/chat/api/support/conversations/{conv.id}/assign"
        )
        assert r.status_code == 409
        assert r.json()["localizable_error"] == "error.409.chat_already_assigned"

    def test_resolve_then_reopen(self, api_client, user, operator_user):
        conv = _support(user)
        client = _op_client(api_client, operator_user)
        client.post(f"/chat/api/support/conversations/{conv.id}/assign")
        r = client.post(f"/chat/api/support/conversations/{conv.id}/resolve")
        assert r.status_code == 200
        assert r.json()["support_status"] == SupportStatus.RESOLVED
        r = client.post(f"/chat/api/support/conversations/{conv.id}/reopen")
        assert r.status_code == 200
        assert r.json()["support_status"] == SupportStatus.OPEN

    def test_resolve_requires_operator_role(self, api_client, user):
        # The customer (member, not operator) cannot resolve.
        conv = _support(user)
        api_client.force_authenticate(user=user)
        r = api_client.post(f"/chat/api/support/conversations/{conv.id}/resolve")
        assert r.status_code == 403
        assert r.json()["localizable_error"] == "error.403.chat_not_operator"


@pytest.mark.django_db
class TestSupportDisabled:
    def test_queue_and_assign_disabled(self, api_client, operator_user, settings):
        settings.STAPEL_CHAT = {"CHAT_KINDS": ["direct", "group"]}
        client = _op_client(api_client, operator_user)
        assert client.get("/chat/api/support/queue").status_code == 400
        import uuid

        r = client.post(f"/chat/api/support/conversations/{uuid.uuid4()}/assign")
        assert r.status_code == 400
        assert r.json()["localizable_error"] == "error.400.chat_kind_disabled"


@pytest.mark.django_db
class TestSupportServiceGuards:
    def test_assign_rejects_non_support(self, user, operator_user):
        conv = services.create_group(owner=user)
        with pytest.raises(services.NotSupport):
            services.assign_operator(conversation=conv, operator=operator_user)
