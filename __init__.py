"""stapel-chat — conversations, messaging and support chat for Stapel.

A generic messaging core: ``Conversation`` (direct / group / support),
``Message`` (monotonic per-conversation ``seq``, text or system), per-participant
read markers, an idempotent direct-conversation primitive, anchor-paginated
history and conversation lists, and a support layer (queue → assign → resolve →
reopen) built on the same model. Realtime delivery is an optional Channels
consumer; correctness never depends on it (clients replay by ``seq``).
"""

default_app_config = "stapel_chat.apps.ChatConfig"
