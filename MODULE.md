# stapel-chat — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it without
> forking, and what not to do. Kept in the same PR as any change to a seam. See
> also README.md and CHANGELOG.md.

## What this module provides

- **Conversation / Participant / Message** — the generic messaging core. One
  `Conversation` model, three `kind`s: `direct` (1:1), `group`, `support`.
  `ConversationParticipant` holds a `role` (`member` / `operator`) and a
  `last_read_seq` read marker. `Message` carries a per-conversation monotonic
  `seq`, a `kind` (`text` / `system`), a `body`, an optional `reply_to`, and a
  list of opaque `attachments` keys. There is **no FK to Organization/Workspace**
  (scope is the opaque `scope_key`) and **no file storage** (a message stores
  attachment *keys*; the bytes live in the host's CDN).
- **Monotonic seq** — the send path locks the conversation row, allocates
  `last_seq + 1`, persists it and the message, all under one transaction; the
  `(conversation, seq)` unique constraint + a retry loop is the backstop for
  backends without row locking. seq is gapless and total — the canonical anchor
  for history and the resume cursor for realtime.
- **Direct idempotency** — a direct thread is keyed by an order-independent
  `direct_key` over the participant pair (namespaced by scope), uniquely
  constrained among direct threads. Get-or-create; the create race is resolved
  by the constraint (loser returns the winner's row).
- **Read markers & unread** — `mark_read` advances `last_read_seq` (never
  backwards); `unread_count` is messages newer than the marker authored by
  someone else (system lines never raise a badge).
- **History & lists** — anchor-paginated (core `AnchorPagination`). Message
  history anchors on `seq`, newest-first, both directions; the conversation list
  anchors on `updated_at` and reports `unread_count`.
- **Support layer** — the unassigned queue (`support_queue`), first-come
  `assign_operator` (adds the operator participant, emits
  `chat.support.assigned`, posts a system line), and `open` / `pending` /
  `resolved` with `reopen` — all on the same model (`kind=support`).
- **Realtime (optional)** — `stapel_chat.consumers.ChatConsumer` over
  `stapel_core.django.jwt.channels`. Store-first, transport-thin: the socket
  never owns state; it relays the durable, `seq`-ordered journal. `hello{last_seq}`
  replays `Message.seq > last_seq` then goes live; frames are seq-deduped;
  a too-wide gap answers `error{resync}`. **Correctness never depends on
  delivery** — a client recovers by replaying from the rows.

## Extension points (fork-free)

### 1. Message hook — `chat.message` (comm emit)

Every appended message emits `chat.message` (transactionally, via the outbox).
Realtime delivery, a search indexer, a notifier — all subscribe without any
coupling in the engine. Schema: `schemas/emits/chat.message.json`.

### 2. Support-assignment hook — `chat.support.assigned` (comm emit)

Emitted when an operator claims a support thread. Routing / operator-notification
layers subscribe. Schema: `schemas/emits/chat.support.assigned.json`.

### 3. scope_key provider — `SCOPE_PROVIDER` (dotted path, replace)

A `ScopeProvider` (`resolve(request) -> scope_key`, `filter(qs, request)`)
resolves the opaque scope from the request and filters querysets. Default is a
no-op single global scope; a host may return the active `workspace_id` to
partition conversations per tenant.

### Serializer seams (`views.py`)

`SerializerSeamMixin` — subclass a view, set `request_serializer_class` /
`response_serializer_class`, remount the URL.

### Settings — `STAPEL_CHAT` namespace (`conf.py`)

Resolution order per key: `settings.STAPEL_CHAT[key]` -> flat Django setting ->
environment variable -> default. Read lazily at call time.

| Key | Default | What it customizes | Semantics |
|---|---|---|---|
| `CHAT_KINDS` | `["direct","group","support"]` | Enabled conversation kinds | **axis** (list; drop `support` to disable the queue) |
| `ATTACHMENTS` | `True` | Whether messages may carry attachment keys | **axis** (bool) |
| `MAX_BODY_LENGTH` | `4000` | Hard cap on a text body (chars) | **axis** (int) |
| `SCOPE_PROVIDER` | `stapel_chat.scope.DefaultScopeProvider` | Scope resolution/filtering | replace (dotted path) |

`CHAT_KINDS`, `ATTACHMENTS` and `MAX_BODY_LENGTH` are the three CTO-facing config
axes surfaced in `docs/capabilities.json`. They are **behavioral, not URL
gates**: they narrow what a request may create/operate/carry (enforced in the
views), they never unmount an endpoint.

### Events (comm surface)

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Emit | `chat.message` | `{message_id, conversation_id, conversation_kind, scope_key, sender_id?, seq, kind, body, reply_to?, attachments, created_at}` | `schemas/emits/chat.message.json` |
| Emit | `chat.support.assigned` | `{conversation_id, operator_id, scope_key}` | `schemas/emits/chat.support.assigned.json` |
| Consume | `user.deleted` | `{user_id, ...}` | `schemas/consumes/user.deleted.json` |

### Realtime protocol (`consumers.py`)

```
client → server:  hello{last_seq} / send{body,attachments,reply_to} / ack{seq} / ping
server → client:  welcome{server_seq} / message{…seq} / replay_done{up_to_seq} / error{code,message} / pong
```

## Anti-patterns

- **Don't order messages by timestamp.** `seq` is the total order; two messages
  in the same millisecond still have a definite order and a stable anchor.
- **Don't allocate seq outside the locked send path.** Always go through
  `services.post_message` — it holds the row lock and the retry backstop.
  Hand-inserting a `Message` with a chosen `seq` bypasses the counter.
- **Don't store files here.** `attachments` are opaque keys into the host's CDN;
  the module never sees bytes.
- **Don't depend on realtime for correctness.** The socket is a convenience over
  the durable journal — clients reconcile by replaying `seq`.
- **Don't emit outside the transaction.** Use `mutate_and_emit()` so the row and
  the `chat.message` event commit together (the emit-check gate enforces this).
- **Don't import other stapel modules** — cross-module is comm by string name.
- **Don't bypass the settings namespace** with `os.getenv` at import time.

## Contract emission — the `schema` + `flows` + `errors` + `capabilities` set

This module emits its **own** machine-readable API contract, per-module
(contract-pipeline.md §2). Chat is not yet mounted in stapel-example-monolith,
so there is no aggregate slice to diff against for byte-identity; standalone
validation substitutes (contract-pipeline.md §9 fallback): determinism,
self-contained `$ref` closure, `JWTCookieAuth` security on every protected
operation, canonical `/chat/api/*` prefix. `tests/test_contract.py` asserts all
of it.

Regenerate after any serializer/view/url/error/axis change:

    make contract        # or: python -m stapel_chat._codegen --out docs

then commit `docs/{schema,flows,errors,capabilities}.json`.

## App-layer override vs upstream contribution — rule of thumb

**App-layer** (host project, no fork) if the change fits a seam above: a settings
key, a subclass + URL remount, a comm subscriber.

**Upstream contribution** if it needs new model fields/migrations, new endpoints,
a new settings key or seam, or changes a committed schema.
