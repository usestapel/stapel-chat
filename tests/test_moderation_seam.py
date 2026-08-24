"""The moderation seam: a message answers for itself, and a tombstone is gone.

Until this release the only way to complain about a chat message anywhere in
the fleet was stapel-classified's EVIDENCE-based policy — the reporter's own
screenshot, marked unverified, because no module served a message's content.
This module stores every message it delivers, so that was never true. The
tests here are the two halves of making it true: the content function answers
with the live message and its author, and a message that no longer exists
says so instead of answering with the empty body a tombstone leaves behind.
"""
import pytest

from stapel_chat import services
from stapel_chat.moderation import (
    MESSAGE_TARGET_POLICY,
    MESSAGE_TARGET_TYPE,
    register_moderation_target,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_moderation_registry():
    """Target types are process-global; leave the registry as we found it."""
    from stapel_moderation.registry import reset_registries

    reset_registries()
    yield
    reset_registries()


def _direct(user, other):
    return services.create_direct(owner=user, other_user_id=other.id)


# ── The content function ─────────────────────────────────────────────


def test_a_message_answers_with_its_text_and_its_author(user, other_user):
    """``author_id`` is the load-bearing field: it is what makes "you cannot
    report your own content" and "sanction the right person" answerable
    without trusting anything the client sent."""
    from stapel_core.comm import call

    conv = _direct(user, other_user)
    msg = services.post_message(
        conversation=conv, sender=other_user, body="meet me off-platform"
    )

    answer = call("chat.moderation_content", {"message_id": str(msg.id)})
    assert answer["text"] == "meet me off-platform"
    assert answer["author_id"] == str(other_user.pk)
    assert answer["conversation_id"] == str(conv.id)
    assert answer["conversation_kind"] == conv.kind
    assert answer["seq"] == msg.seq
    assert answer["edited"] is False


def test_the_card_reads_the_message_as_it_is_now_not_as_it_was(user, other_user):
    """Content is fetched when it is LOOKED at. An author who edits after the
    complaint does not get to be judged on the text they replaced."""
    from stapel_core.comm import call

    conv = _direct(user, other_user)
    msg = services.post_message(conversation=conv, sender=other_user, body="original")
    services.edit_message(message=msg, editor=other_user, body="edited")

    answer = call("chat.moderation_content", {"message_id": str(msg.id)})
    assert answer["text"] == "edited"
    assert answer["edited"] is True


def test_attachment_keys_ride_in_media(user, other_user, settings):
    conv = _direct(user, other_user)
    msg = services.post_message(
        conversation=conv,
        sender=other_user,
        body="",
        attachments=[{"key": "cdn-key-1", "type": "image"}],
    )

    from stapel_core.comm import call

    answer = call("chat.moderation_content", {"message_id": str(msg.id)})
    assert answer["media"] == ["cdn-key-1"]
    # Opaque CDN handles are not images a vision screener can read.
    assert MESSAGE_TARGET_POLICY["media"] is False


def test_a_message_that_never_existed_is_a_lookup_error(user, other_user):
    import uuid

    with pytest.raises(services.MessageNotFound):
        services.moderation_content(uuid.uuid4())


# ── Tombstones and erasure ───────────────────────────────────────────


def test_a_deleted_message_is_gone_not_empty(user, other_user):
    """A tombstone's body is empty by construction. Handing that back would
    show a moderator a blank card indistinguishable from a message that said
    nothing at all."""
    conv = _direct(user, other_user)
    msg = services.post_message(conversation=conv, sender=other_user, body="slur")
    services.delete_message(message=msg, actor=other_user)

    with pytest.raises(services.MessageNotFound):
        services.moderation_content(msg.id)


def test_erasure_takes_the_content_out_of_the_moderation_card_too(user, other_user):
    """The right to erasure does not stop at this module's border: a case
    must not become the one place the erased text survives."""
    conv = _direct(user, other_user)
    msg = services.post_message(conversation=conv, sender=other_user, body="erase me")

    services.erase_user_messages(other_user.pk)

    with pytest.raises(services.MessageNotFound):
        services.moderation_content(msg.id)


# ── Registration into stapel-moderation ──────────────────────────────


def test_the_target_registers_and_moderation_can_read_a_message(user, other_user):
    """The seam end to end, through stapel-moderation's own reader: the queue
    knows nothing about chat, calls a name, and gets the message."""
    from stapel_moderation import services as moderation

    assert register_moderation_target() is True

    conv = _direct(user, other_user)
    msg = services.post_message(conversation=conv, sender=other_user, body="fraud")

    content = moderation.fetch_content(MESSAGE_TARGET_TYPE, str(msg.id))
    assert content.text == "fraud"
    assert content.author_id == str(other_user.pk)
    assert content.extra["conversation_id"] == str(conv.id)


def test_moderation_reads_a_tombstone_as_target_not_found(user, other_user):
    """404, not 503: nothing is down, there is nothing left to look at. The
    difference decides whether a case is dismissed or parked waiting for a
    row that is never coming back."""
    from stapel_moderation import services as moderation

    register_moderation_target()

    conv = _direct(user, other_user)
    msg = services.post_message(conversation=conv, sender=other_user, body="gone soon")
    services.delete_message(message=msg, actor=other_user)

    with pytest.raises(moderation.TargetNotFound):
        moderation.fetch_content(MESSAGE_TARGET_TYPE, str(msg.id))


def test_a_host_declaration_wins_over_ours(settings):
    """The runtime registry layer outranks settings, so registering
    unconditionally would silently overwrite a composite's deliberate policy
    — stapel-classified declares this very type. Fill the gap, never
    overrule the host."""
    from stapel_moderation.registry import resolve_policy

    settings.STAPEL_MODERATION = {
        "TARGET_TYPES": {
            MESSAGE_TARGET_TYPE: {
                "gate": "post",
                "evidence": True,
                "verdict_event": None,
                "reasons": ["harassment"],
            }
        }
    }

    assert register_moderation_target() is False
    policy = resolve_policy(MESSAGE_TARGET_TYPE)
    assert policy["evidence"] is True
    assert policy["content_function"] == ""
    assert policy["reasons"] == ["harassment"]


def test_registering_a_second_time_is_a_no_op():
    assert register_moderation_target() is True
    assert register_moderation_target() is False


def test_the_empty_setting_registers_nothing(settings):
    from stapel_moderation.registry import get_target_types

    settings.STAPEL_CHAT = {"MODERATION_TARGET_TYPE": ""}

    assert register_moderation_target() is False
    assert MESSAGE_TARGET_TYPE not in get_target_types()


def test_the_registered_policy_names_a_reachable_function():
    """Declared AND connected — the "declared but never wired" defect the
    moderation module exists to catch, asserted on our own registration."""
    from stapel_core.comm import function_unreachable_reason
    from stapel_moderation.registry import resolve_policy

    register_moderation_target()
    policy = resolve_policy(MESSAGE_TARGET_TYPE)

    assert policy["content_function"] == "chat.moderation_content"
    assert policy["id_field"] == "message_id"
    assert not function_unreachable_reason("chat.moderation_content")
    # Nothing in the fleet applies a verdict to a message: explicit None is a
    # statement (moderation announces it as W006), not an omission.
    assert policy["verdict_event"] is None


# ── The composite key a composite names a message by ─────────────────


def test_the_composite_key_is_served_and_its_halves_must_agree(user, other_user):
    """stapel-classified names a message ``<conversation_id>:<message_id>``
    so that WHO may report it is answerable off its own conversation table —
    nobody can answer that from a bare message id. Both spellings are served,
    and a message quoted under somebody else's conversation is not this
    target."""
    conv = _direct(user, other_user)
    other = services.create_group(owner=user, participant_ids=[other_user.id])
    msg = services.post_message(conversation=conv, sender=other_user, body="hello")

    answer = services.moderation_content(f"{conv.id}:{msg.id}")
    assert answer["text"] == "hello"
    assert answer["conversation_id"] == str(conv.id)

    with pytest.raises(services.MessageNotFound):
        services.moderation_content(f"{other.id}:{msg.id}")


def test_a_key_that_is_not_an_id_at_all_is_not_found(user, other_user):
    """A malformed key is "no such message", not a 500 that reads to
    moderation as the owner being down."""
    with pytest.raises(services.MessageNotFound):
        services.moderation_content("not-a-uuid")
    with pytest.raises(services.MessageNotFound):
        services.moderation_content("also:not-a-uuid")
