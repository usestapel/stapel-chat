"""comm surface of stapel-chat.

Every emit carries a JSON schema in ``schemas/`` — tests run with
``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails loudly.
Emit schemas are auto-registered from ``schemas/emits/`` at startup
(``autoload_schemas``); this module is imported from ``apps.py:ready()`` for
symmetry with the rest of the shelf and as the single place documenting the
surface.

Emits (see schemas/emits/):
- ``chat.message`` — a message was appended to a conversation (written into the
  outbox in the same transaction as the row). Realtime consumers and any
  downstream (search indexer, notifier) subscribe.
- ``chat.support.assigned`` — a support conversation was assigned to an
  operator. Routing/notification layers subscribe.
- ``chat.conversation.created`` — a thread was opened. Emitted only on a real
  create (an idempotent ``create_direct`` that returned an existing thread is
  not one), so a consumer may bind a domain object to it exactly once.

Functions (see schemas/functions/):
- ``chat.moderation_content`` — a message's content for an external moderation
  module's screening and moderator card. Identifiers travel on the bus;
  content is fetched at the moment it is looked at. Named as the
  ``content_function`` of the ``chat_message`` target type (moderation.py).
- ``chat.conversation_participants`` — who is a party to these conversations,
  batched, answering for every id asked. The read a consumer had to avoid by
  keeping its own copy of the parties on its own row.
- ``chat.post_system_message`` — one system line into a conversation, written
  by another service. The narrow WRITE in a family of reads, and narrow on
  purpose: it can post only with ``sender=None`` and ``kind=system``. A
  general ``chat.post_message`` on the bus would be a way to put words in a
  user's mouth, in a product where the thread is the record of a deal.

Consumes (see schemas/consumes/):
- ``user.deleted`` — erase the deleted user's messages and participations
  (see actions.py / gdpr.py).
"""
import json
from pathlib import Path

from stapel_core.comm import function

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas" / "functions"


def _schema(name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


@function(
    "chat.post_system_message", schema=_schema("chat.post_system_message")
)
def post_system_message(payload):
    """Write one system line into a conversation, on behalf of no user.

    Input: ``{"conversation_id": str, "body": str, "client_msg_id": str?}``.
    Output: ``{"message_id", "seq", "conversation_id"}``.

    The one WRITE on this surface, and the reason it is this shape rather than
    a general "post a message": it hard-codes ``sender=None`` and
    ``kind=system``, so nothing reachable from the bus can say something *as
    a person*. The audience is a sibling service recording that something
    happened in a thread it does not own — a call ended, a booking was
    confirmed, an order shipped — where the alternative today is that service
    importing ``stapel_chat.services``, which is the import this shelf does
    not permit.

    ``body`` is a marker key the reader renders, following the convention this
    module's own lines already use (``chat.support.assigned``,
    ``video.call.ended:188``). Not a rendered sentence: the reader's locale is
    not the writer's, and a formatted duration freezes one presentation into
    a row that outlives it.

    ``client_msg_id`` is the existing per-conversation idempotency key. A
    caller on an at-least-once transport passes one derived from the fact it
    is recording, and a redelivery writes one line rather than two.

    An unknown conversation raises ``services.ConversationNotFound`` (a
    ``LookupError``), so a caller can tell "that thread is gone, stop
    retrying" from "I could not answer, retry" — the same contract the
    ``*.moderation_content`` family established. Answering a quiet success
    would make a service that lost its thread reference look exactly like one
    whose writes are landing.
    """
    from . import services

    message = services.post_system_message(
        payload["conversation_id"],
        payload["body"],
        payload.get("client_msg_id") or "",
    )
    return {
        "message_id": str(message.pk),
        "seq": message.seq,
        "conversation_id": str(message.conversation_id),
    }


@function("chat.moderation_content", schema=_schema("chat.moderation_content"))
def moderation_content(payload):
    """Return a message's content for an external moderation module.

    Input: ``{"message_id": str}`` — the moderation case's ``target_key`` for
    a ``chat_message`` target.
    Output: ``{"text", "title", "language", "media", "author_id", "url",
    "kind", "conversation_id", "conversation_kind", "scope_key", "seq",
    "edited", "created_at"}``.

    A deleted or erased message raises ``services.MessageNotFound`` (a
    ``LookupError``): a tombstone is gone, not empty.
    """
    from . import services

    return services.moderation_content(payload["message_id"])


@function(
    "chat.conversation_participants",
    schema=_schema("chat.conversation_participants"),
)
def conversation_participants(payload):
    """Return the parties to each conversation asked about.

    Input: ``{"conversation_ids": [str, …]}``.
    Output: ``{"conversations": {id: {"exists", "kind", "scope_key",
    "subject_type", "subject_key", "participants": [{"user_id", "role"}]}}}``.

    Every id supplied comes back, including one that names nothing
    (``exists: false``). Unguarded, like the rest of this family: the bus is
    a trusted boundary, and *who may ask* is the caller's deployment policy.
    """
    from . import services

    return {
        "conversations": services.conversation_participants(
            payload.get("conversation_ids") or []
        )
    }
