"""comm surface of stapel-chat.

Every emit carries a JSON schema in ``schemas/`` — tests run with
``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails loudly.
Emit schemas are auto-registered from ``schemas/emits/`` at startup
(``autoload_schemas``); this module is imported from ``apps.py:ready()`` for
symmetry with the rest of the shelf and as the single place documenting the
surface. stapel-chat provides no synchronous Functions in v0.1.

Emits (see schemas/emits/):
- ``chat.message`` — a message was appended to a conversation (written into the
  outbox in the same transaction as the row). Realtime consumers and any
  downstream (search indexer, notifier) subscribe.
- ``chat.support.assigned`` — a support conversation was assigned to an
  operator. Routing/notification layers subscribe.

Consumes (see schemas/consumes/):
- ``user.deleted`` — erase the deleted user's messages and participations
  (see actions.py / gdpr.py).
"""
