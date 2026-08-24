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

Functions (see schemas/functions/):
- ``chat.moderation_content`` — a message's content for an external moderation
  module's screening and moderator card. Identifiers travel on the bus;
  content is fetched at the moment it is looked at. Named as the
  ``content_function`` of the ``chat_message`` target type (moderation.py).

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
