"""``chat.post_system_message`` — the one WRITE on the comm surface.

Narrow on purpose, and these tests are what keeps it narrow: the payload has
no ``sender`` and no ``kind``, so nothing reachable from the bus can post as a
person. In a product where the thread is the record of a deal, a general
"post a message" Function is not a convenience.
"""
import pytest
from stapel_core.comm import call

from stapel_core.comm.exceptions import FunctionCallError

from stapel_chat.models import Conversation, Message, MessageKind
from stapel_chat.services import ConversationNotFound

pytestmark = pytest.mark.django_db

NAME = "chat.post_system_message"


@pytest.fixture
def direct(user, other_user):
    from stapel_chat import services

    return services.create_direct(owner=user, other_user_id=other_user.pk)


def test_it_writes_a_system_line_with_no_author(direct):
    answer = call(NAME, {"conversation_id": str(direct.pk), "body": "video.call.ended:188"})

    message = Message.objects.get(pk=answer["message_id"])
    assert message.kind == MessageKind.SYSTEM
    # The property the whole shape exists for: nothing on the bus can name an
    # author, so nothing on the bus can post as one.
    assert message.sender_id is None
    assert message.body == "video.call.ended:188"
    assert answer["seq"] == message.seq
    assert answer["conversation_id"] == str(direct.pk)


def test_the_payload_has_no_way_to_name_a_sender(direct, user):
    """A sender field would be the whole risk, so the schema forbids extras.

    ``additionalProperties: false`` is doing real work here: without it a
    hopeful caller passing ``sender_id`` would be silently ignored, which is
    the right behaviour but an accident. With it, the attempt is an error the
    caller sees.
    """
    from stapel_core.comm.exceptions import SchemaValidationError

    with pytest.raises((SchemaValidationError, FunctionCallError, ValueError)):
        call(
            NAME,
            {
                "conversation_id": str(direct.pk),
                "body": "video.call.ended:1",
                "sender_id": str(user.pk),
            },
        )
    assert Message.objects.count() == 0


def test_the_same_client_msg_id_writes_one_line(direct):
    """At-least-once delivery must not produce two lines.

    The caller derives the key from the fact it is recording (a call id, an
    order id), so a redelivery collides with itself.
    """
    payload = {
        "conversation_id": str(direct.pk),
        "body": "video.call.missed",
        "client_msg_id": "video-call-abc",
    }
    first = call(NAME, payload)
    second = call(NAME, payload)
    assert first["message_id"] == second["message_id"]
    assert Message.objects.filter(conversation=direct).count() == 1


def test_an_unknown_conversation_raises_rather_than_answering_quietly(direct):
    """A caller that could not record what happened has to find out.

    ``LookupError`` and not a no-op: a service that lost its thread reference
    would otherwise look exactly like one whose writes are landing.
    """
    import uuid

    with pytest.raises(FunctionCallError) as raised:
        call(NAME, {"conversation_id": str(uuid.uuid4()), "body": "video.call.missed"})
    # The comm layer wraps a provider failure, and the CAUSE is what carries
    # the distinction a caller acts on: a LookupError means "stop retrying".
    assert isinstance(raised.value.__cause__, ConversationNotFound)
    assert isinstance(raised.value.__cause__, LookupError)


def test_a_malformed_conversation_id_is_the_same_answer(direct):
    with pytest.raises(FunctionCallError) as raised:
        call(NAME, {"conversation_id": "not-a-uuid", "body": "video.call.missed"})
    assert isinstance(raised.value.__cause__, ConversationNotFound)


def test_a_blank_body_is_refused(direct):
    with pytest.raises(FunctionCallError):
        call(NAME, {"conversation_id": str(direct.pk), "body": "   "})
    assert Message.objects.count() == 0


def test_a_block_between_the_two_parties_does_not_suppress_the_line(
    direct, user, other_user, settings
):
    """A system line is a statement of fact, not somebody reaching somebody.

    There is no sender for a block to be about, and two people who blocked
    each other still need to see that their call was declined — suppressing
    it would leave the thread silent about something that did happen.
    """
    from stapel_core.comm import function, function_registry

    name = "test.relationships_all_blocked"

    @function(name)
    def _blocked(payload):
        return {"blocked": list(payload.get("pairs") or [])}

    settings.STAPEL_CHAT = {
        **getattr(settings, "STAPEL_CHAT", {}),
        "BLOCK_ENFORCEMENT": "on",
        "BLOCK_FUNCTION": name,
    }
    try:
        answer = call(
            NAME, {"conversation_id": str(direct.pk), "body": "video.call.declined"}
        )
    finally:
        function_registry._providers.pop(name, None)
    assert Message.objects.get(pk=answer["message_id"]).kind == MessageKind.SYSTEM


def test_the_line_reaches_the_conversation_readers(direct):
    """It goes through post_message, so the fan-out is not a second path."""
    call(NAME, {"conversation_id": str(direct.pk), "body": "video.call.ended:5"})
    conv = Conversation.objects.get(pk=direct.pk)
    assert conv.last_seq >= 1


def test_a_system_line_does_not_count_as_unread(direct, user, other_user):
    """Pre-existing behaviour, restated because this Function multiplies it.

    ``unread_count`` excludes authorless messages. A call that ends is not a
    message somebody has to read, and a badge that counts them would put a
    "1" on a thread nobody wrote in.
    """
    from stapel_chat import services
    from stapel_chat.models import ConversationParticipant

    call(NAME, {"conversation_id": str(direct.pk), "body": "video.call.ended:5"})
    participant = ConversationParticipant.objects.get(
        conversation=direct, user=other_user
    )
    assert services.unread_count(conversation=direct, participant=participant) == 0
