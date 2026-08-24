# stapel-chat — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it without
> forking, and what not to do. Kept in the same PR as any change to a seam. See
> also README.md and CHANGELOG.md.

## What this module provides

**Realtime is the module, not a mode of it.** A message is sent and received
over a WebSocket; REST serves history, hydration and the operations a socket
should not own. A deployment that cannot serve the socket fails `manage.py
check` (`stapel_chat.E010`–`E014`) rather than degrading into a product that
refreshes on a timer — see *Why there is no realtime switch* below.

- **Conversation / Participant / Message** — the generic messaging core. One
  `Conversation` model, three `kind`s: `direct` (1:1), `group`, `support`.
  `ConversationParticipant` holds a `role` (`member` / `operator`) and two
  markers, `last_read_seq` and `last_delivered_seq`. `Message` carries two
  sequences (below), a `kind` (`text` / `system`), a `body`, an optional
  `reply_to`, a `client_msg_id`, `edited_at` / `deleted_at`, and a list of
  attachment descriptors. There is **no FK to Organization/Workspace** (scope is
  the opaque `scope_key`) and **no file storage** (a message stores an opaque
  CDN key plus render metadata; the bytes live in the host's CDN).
- **Two sequences, and confusing them is the classic bug.**
  - `seq` — the message's **position in the thread**. Allocated once under a row
    lock from `Conversation.last_seq`, never touched again. Gapless and total:
    the history anchor and the client's sort key.
  - `rev_seq` — its position in the conversation's **revision journal**. Starts
    equal to `seq`, re-allocated from the same counter on every edit and delete.
    Realtime replay is anchored on `rev_seq`, which is the only reason an edit
    made while a client was offline can ever reach it: anchored on `seq` the row
    sits behind a cursor the client already acknowledged.

  **A client upserts by `message_id`, orders by the payload's `seq`, and treats
  the envelope's `seq` purely as a resume cursor.**
- **Deletion is a tombstone.** `deleted_at` is stamped, `body` and `attachments`
  are emptied, and the row keeps being delivered under a fresh `rev_seq` —
  precisely so every client cache and offline database learns *which id to
  purge*. An id that stops arriving is an id nobody can purge. **Retention:
  permanent.** Unlike core's JWT tombstone there is no bounded credential
  lifetime to expire against — an offline chat cache can be months stale, so no
  TTL is safe — and what survives is an id, a sequence and two timestamps. It
  also keeps `seq` gapless and `reply_to` resolvable. Erasure is the one path
  that removes content, and it tombstones too.
- **Idempotent send** — `client_msg_id` is unique per conversation; a retry after
  a dropped socket returns the first row instead of posting a second. There is
  **no draft concept** anywhere in the contract, deliberately: a compose box is
  client state, and a save round trip between the Enter key and the message is
  the thing that must not exist.
- **Attachments that render on first paint** — an opaque CDN `key` plus the
  metadata a bubble needs without a second round trip and without reflow:
  `aspect` / `bytes` / `preview_b64` (a ~16px webp data URI) for images and
  GIFs; those plus `duration_ms` and a poster for video; `duration_ms` and
  `waveform_b64` (a waveform **image**) for voice; `mime` / `ext` / `name` for
  documents. The type set is an **open registry** (below). The metadata itself
  comes from `stapel-cdn` by comm, once, at send time — never re-derived here.
- **Direct idempotency** — a direct thread is keyed by an order-independent
  `direct_key` over the participant pair (namespaced by scope), uniquely
  constrained among direct threads. Get-or-create; the create race is resolved
  by the constraint (loser returns the winner's row).
- **Read + delivery markers** — both move forward only. `unread_count` is
  messages newer than the read marker authored by someone else; system lines and
  tombstones never raise a badge. Both markers ride back with the conversation,
  which is what lets the live receipt be an ephemeral Signal rather than an
  event anyone is owed.
- **History & lists** — anchor-paginated (core `AnchorPagination`). Message
  history anchors on `seq`, newest-first, both directions, tombstones included;
  the conversation list anchors on `updated_at` and reports `unread_count`.
  Every conversation carries its own `stream_key` and `socket_path`.
- **Support layer** — the unassigned queue (`support_queue`), first-come
  `assign_operator` (adds the operator participant, emits
  `chat.support.assigned`, posts a system line), and `open` / `pending` /
  `resolved` with `reopen` — all on the same model (`kind=support`).
- **Two sockets** (`consumers.py`, both built on `stapel-realtime`):
  - `ChatConsumer` → `chat:conv:<id>`, **resumable**. `hello{last_seq}` replays
    `rev_seq > last_seq` then goes live, seq-deduplicated, with a bounded window
    and a `resync` verdict beyond it. Writes (`send` / `edit` / `delete` /
    `read` / `delivered` / `activity`) go through the same service layer the REST
    views call.
  - `ChatInboxConsumer` → `chat:user:<id>`, **ephemeral**. It exists because a
    conversation list with no socket refreshes on a timer forever, however live
    the open thread is. Its stream key comes from the authenticated scope, so
    the route carries no user segment to tamper with.

  **Correctness never depends on delivery** — every durable fact is replayable
  by `rev_seq`; every ephemeral one (typing, receipts, the inbox nudge) is
  recoverable by a REST refetch. That is the substrate's own condition for a
  fact being allowed to travel as a Signal.

## Why there is no realtime switch

A live product's chat was opened and Enter did not send; the panel said
"updates every few seconds". Sockets had been built, mounted, proxied and
smoke-tested, and the product polled anyway.

The cause was a **credential-channel mismatch**. A browser cannot set an
`Authorization` header on `new WebSocket()`. The deployment authenticated HTTP
with an httpOnly JWT cookie; the Channels middleware read only the Authorization
header, the `Sec-WebSocket-Protocol` subprotocol and `?token=`. Every browser
handshake closed 4401, the client treated 4401 as a permanent refusal, and the
seam fell through to its polling half — permanently, silently, and looking
exactly like a product decision.

A polling fallback a product can end up in silently is itself the defect: it is
what made "websockets are done" a false claim. So there is no `REALTIME =
False`. A knob would reproduce the defect, because the deployment that shipped
"updates every few seconds" never chose it either.

## Extension points (fork-free)

### 1. Message hook — `chat.message` (comm emit)

Every appended message emits `chat.message` (transactionally, via the outbox).
Realtime delivery, a search indexer, a notifier — all subscribe without any
coupling in the engine. Schema: `schemas/emits/chat.message.json`.

### 2. Support-assignment hook — `chat.support.assigned` (comm emit)

Emitted when an operator claims a support thread. Routing / operator-notification
layers subscribe. Schema: `schemas/emits/chat.support.assigned.json`.

### 3. scope_key provider — `SCOPE_PROVIDER` (dotted path, replace)

A `ScopeProvider` (`resolve(request) -> scope_key`, `filter(qs, request)`,
`can_operate(request, conversation=None) -> bool`) resolves the opaque scope
from the request, filters querysets, and answers whether the caller may act as
a support **operator** — read the queue, claim a thread, resolve/reopen it.

The shipped `DefaultScopeProvider` is a single global scope for
resolve/filter, but `can_operate` is not a no-op: it answers with the third
principal state (`stapel_core.django.scope`), so a registered account holding
no mandate in any workspace is not an operator. The customer half is
deliberately untouched — a person opening a support ticket typically holds no
mandate at all. A lookup that cannot be answered raises 503, never a 403.

Guarded by system checks `E001`/`E002` (importable, correctly typed) and
`E005`/`W001` — running the shipped single-scope provider is an ERROR where
this deployment has workspaces, a warning where it is genuinely standalone. A
host returns the active `workspace_id` and checks a real operator capability.

`E005` only fires where that "has workspaces" question is provable at boot
(`FUNCTION_TRANSPORT` `inprocess`/`http`). Over a bus transport (`nats`, or a
dotted custom transport) the answer is unknowable at boot by design — `comm.
function_unreachable_reason`'s own docstring says nothing there can, or
should, prove a remote provider is up — so the check downgrades to `W002`
instead of asserting a fact it cannot prove.

### Serializer seams (`views.py`)

`SerializerSeamMixin` — subclass a view, set `request_serializer_class` /
`response_serializer_class`, remount the URL.

### Settings — `STAPEL_CHAT` namespace (`conf.py`)

Resolution order per key: `settings.STAPEL_CHAT[key]` -> flat Django setting ->
environment variable -> default. Read lazily at call time.

| Key | Default | What it customizes | Semantics |
|---|---|---|---|
| `CHAT_KINDS` | `["direct","group","support"]` | Enabled conversation kinds | **axis** (list; drop `support` to disable the queue) |
| `ATTACHMENTS` | `True` | Whether messages may carry attachments | **axis** (bool) |
| `MAX_BODY_LENGTH` | `4000` | Hard cap on a text body (chars) | **axis** (int) |
| `ATTACHMENT_TYPES` | `{}` | The attachment-type registry | **merge over builtins** (`None` removes) |
| `ACTIVITY_STATES` | `{}` | The activity-state registry | **merge over builtins** (`None` removes) |
| `ATTACHMENT_METADATA` | `"cdn"` | `cdn` asks `cdn.describe`; `client` trusts the sender | **axis** (enum) |
| `MAX_ATTACHMENTS` | `10` | Attachments per message | **axis** (int) |
| `MAX_PREVIEW_B64_BYTES` | `8192` | Ceiling on an inline `data:` preview | **axis** (int) |
| `EDIT_WINDOW_S` | `0` | Seconds an author may still edit (0 = forever) | **axis** (int) |
| `SCOPE_PROVIDER` | `stapel_chat.scope.DefaultScopeProvider` | Scope resolution/filtering | replace (dotted path) |

**There is no realtime key.** See *Why there is no realtime switch*.

The axes are **behavioral, not URL gates**: they narrow what a request may
create/operate/carry (enforced in the views), they never unmount an endpoint.

### 4. Attachment types — an OPEN registry (`attachments.py`)

Builtins <- `STAPEL_CHAT["ATTACHMENT_TYPES"]` <- `register_attachment_type()`,
later wins, `None` removes — the same idiom as every other Stapel registry.
Builtins: `image`, `gif`, `video`, `voice`, `file`. Each entry declares
`fields` (what a UI may expect populated for the type — a rendering contract,
not a validation rule, because the CDN generates thumbnails asynchronously and
an image whose thumbnail is not ready yet must still be sendable) and `media`
(whether the ref resolves to a CDN asset worth describing).

Stickers are the named next type. Adding one is:

```python
STAPEL_CHAT = {"ATTACHMENT_TYPES": {
    "sticker": {"fields": ("mime", "bytes", "width", "height", "pack_id"),
                "media": True},
}}
```

An **unregistered** type is refused (400 / `error.400.chat_unknown_attachment_type`).
An open registry is not a free-for-all: the point of registering a type is that
every subscriber knows what it may be asked to render. **Unknown *fields*, by
contrast, are carried through verbatim** — that is what lets the CDN add a
`blurhash` without a release here.

### 5. Activity states — an OPEN registry (`activity.py`)

Same merge semantics. Builtins: `idle`, `typing`, `recording_audio`,
`sending_video`, `uploading_file`. Each carries `ttl_s`, the client's expiry
hint — there is no "stop typing" obligation, because a state that expires on its
own is the only design that survives a tab closed mid-word.

### 6. The CDN contract — what chat asks for and does not build

Chat calls **`cdn.describe`** (`stapel_core.comm.call`, never an import) once
per attachment at send time and merges the answer *over* the client's fields.
The keys it consumes:

| Key | Used for |
|---|---|
| `mime`, `bytes` | every type |
| `width`, `height`, `aspect` | image / gif / video — `aspect` is what reserves the box before the asset lands, i.e. what stops the list reflowing |
| `preview_b64` | the ~16px webp micro-thumbnail as a `data:` URI; **the video poster frame uses the same slot** |
| `duration_ms` | voice, video |
| `waveform_b64` | voice — a waveform **image** as a `data:` URI, so the client paints one `<img>` rather than running a canvas loop |
| `variants[]` | `{tier, branch, url, width, height}` for a responsive `srcset` |

`mime`, `bytes`, `width`, `height`, `aspect`, `duration_ms`, `preview_b64` and
`variants` ship in `cdn.describe` today. **`waveform_b64` and a video
`preview_b64` are the two chat needs and the CDN does not yet produce** — chat
reads them when they appear and falls back to whatever the client supplied
until then. Generation belongs in `stapel-cdn`; a second implementation here
would be a second answer to "how big is this picture".

Failure is open by design: an unknown ref, an unreachable CDN or an unwired comm
transport leaves the client's values in place and logs. A chat message must not
fail to send because a metadata provider blinked; the worst case is a bubble
rendered from the sender's own numbers.

### Events (comm surface)

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Emit | `chat.message` | `{message_id, conversation_id, conversation_kind, scope_key, sender_id?, seq, rev_seq, kind, body, reply_to?, attachments[], client_msg_id?, edited, deleted, created_at}` | `schemas/emits/chat.message.json` |
| Emit | `chat.message.edited` | the same shape, `edited_at` non-null, fresh `rev_seq` | `schemas/emits/chat.message.edited.json` |
| Emit | `chat.message.deleted` | `{message_id, conversation_id, seq, rev_seq, deleted_at, …}` | `schemas/emits/chat.message.deleted.json` |
| Emit | `chat.support.assigned` | `{conversation_id, operator_id, scope_key}` | `schemas/emits/chat.support.assigned.json` |
| Consume | `user.deleted` | `{user_id, ...}` | `schemas/consumes/user.deleted.json` |
| Signal | `chat.read` | `{conversation_id, user_id, last_read_seq}` | ephemeral — no schema, no outbox |
| Signal | `chat.delivered` | `{conversation_id, user_id, last_delivered_seq}` | ephemeral |
| Signal | `chat.activity` | `{conversation_id, user_id, state, ttl_s}` | ephemeral |
| Signal | `chat.inbox` | `{conversation_id, conversation_kind, last_seq, message{…}}` | ephemeral, on `chat:user:<id>` |

Signals are **not** Actions: no outbox, no retry, no history. Delivering
"typing…" five minutes late is worse than not delivering it.

### The wire contract (`consumers.py`, `realtime.py`)

Everything below is `stapel-realtime` wire **v1**. Every frame, both
directions:

```json
{"v": 1, "type": "<name>", "stream": "chat:conv:<uuid>",
 "payload": { }, "seq": 42}
```

`seq` is present on **journal frames only** (`replay`, `live`). Frame kind is
structural, not a flag: a signal physically cannot carry `seq`, because nothing
persisted it. **The envelope's `seq` is `rev_seq` — a resume cursor. The
payload's `seq` is the message's place in the thread — the sort key.**

#### Endpoints

| Path | Consumer | Kind |
|---|---|---|
| `ws/chat/<uuid:conversation_id>` | `ChatConsumer` | resumable journal |
| `ws/chat/inbox` | `ChatInboxConsumer` | ephemeral (no id in the URL — it comes from the token) |

Auth is the substrate's G14 JWT stack, behind an origin guard. Close codes are
the substrate's canon: `4401` unauthenticated, `4403` not a participant, `4404`
unknown stream, `4408` heartbeat timeout, `4410` revoked, `4413` client too
slow. `4401`/`4403`/`4404` are terminal — do not retry with the same
credential. Everything else is a normal reconnect.

#### client → server

| type | payload | notes |
|---|---|---|
| `hello` | `{last_seq}` | `last_seq` is the highest **envelope** seq you hold. `0` = full replay. Re-authorized on every hello. |
| `ping` / `pong` | `{}` | the substrate's heartbeat |
| `send` | `{body, attachments[], reply_to?, client_msg_id?}` | `client_msg_id` makes the retry idempotent and lets you reconcile your optimistic bubble |
| `edit` | `{message_id, body}` | author only |
| `delete` | `{message_id}` | author only; leaves a tombstone |
| `read` | `{upto_seq}` | message seq, not envelope seq |
| `delivered` | `{upto_seq}` | "my client holds this", distinct from read |
| `activity` | `{state}` | a name from the activity registry |

Writes get **no direct ack**. On success the committed row comes back as a
`live` frame to every subscriber including the sender, carrying your
`client_msg_id` — one delivery path, nothing to keep in step. On refusal you get
an `error` frame.

#### server → client

| type | payload | seq |
|---|---|---|
| `welcome` | `{server_seq}` (conversation) / `{ephemeral: true}` (inbox) | — |
| `replay` | a message | ✔ |
| `replay_done` | `{up_to_seq}` | — |
| `live` | a message | ✔ |
| `resync` | `{gap, window, server_seq}` | — — a normal instruction to re-hydrate over REST history, **not** an error; the socket stays open |
| `kick` | `{reason}` | — — followed by close `4410` |
| `error` | `{code, message}` | — |
| `chat.read` | `{conversation_id, user_id, last_read_seq}` | — |
| `chat.delivered` | `{conversation_id, user_id, last_delivered_seq}` | — |
| `chat.activity` | `{conversation_id, user_id, state, ttl_s}` | — |
| `chat.inbox` | `{conversation_id, conversation_kind, last_seq, message}` | — (inbox stream) |

#### the message payload

```json
{
  "message_id": "uuid", "conversation_id": "uuid", "sender_id": "uuid|null",
  "seq": 12, "rev_seq": 19, "kind": "text|system",
  "body": "…", "reply_to": "uuid|null",
  "attachments": [{
    "key": "product/<hash>", "type": "image",
    "mime": "image/webp", "bytes": 91234, "name": null, "ext": null,
    "width": 1200, "height": 800, "aspect": 1.5,
    "duration_ms": null,
    "preview_b64": "data:image/webp;base64,…",
    "waveform_b64": null,
    "variants": [{"tier": 240, "branch": "w", "url": "…", "width": 240, "height": 160}]
  }],
  "client_msg_id": "c-7|null",
  "edited": false, "edited_at": null,
  "deleted": false, "deleted_at": null,
  "created_at": "2026-08-24T…Z"
}
```

Every attachment key is always present, `null` where unknown — a client never
has to test for a field's existence.

#### `error` codes on this socket

Substrate: `bad_envelope`, `bad_type`, `unauthorized`. Chat: `empty`,
`too_long`, `attachments_disabled`, `invalid_attachment`,
`unknown_attachment_type`, `invalid_reply`, `not_found`, `not_author`,
`not_editable`, `deleted`, `unknown_activity`.

#### the client loop, in full

1. `GET /chat/api/v1/conversations` → each row carries `stream_key` and
   `socket_path`. Open `ws/chat/inbox` for the list.
2. Open `socket_path` for the open thread; send `hello{last_seq: <highest
   envelope seq you have cached, else 0>}`.
3. On `welcome` → `replay`* → `replay_done`, you are live. On `resync`, page
   `GET .../messages` instead and hello again with the new cursor.
4. For every `replay` / `live`: **upsert by `message_id`**, sort by
   `payload.seq`, store `envelope.seq` as your cursor. A `deleted` payload means
   purge your local copy of that id.
5. Enter sends a `send` frame with a fresh `client_msg_id` and renders the
   bubble optimistically; the `live` frame that echoes that id replaces it.
6. `read` / `delivered` as the viewport moves; `activity` while composing,
   re-sent no faster than the state's `ttl_s`.

### HTTP surface

Everything above also exists over REST, because rehydration, history paging and
the support lifecycle are not socket work:

```
GET|POST   /chat/api/v1/conversations
GET        /chat/api/v1/conversations/{id}
GET|POST   /chat/api/v1/conversations/{id}/messages
PATCH|DELETE /chat/api/v1/conversations/{id}/messages/{message_id}
POST       /chat/api/v1/conversations/{id}/read        {upto_seq, delivered_upto_seq?}
POST       /chat/api/v1/conversations/{id}/activity    {state}
GET        /chat/api/v1/support/queue
POST       /chat/api/v1/support/conversations/{id}/{assign,resolve,reopen}
```

`DELETE` answers **200 with the stripped message**, not 204 — the caller is
handed the exact row shape that says "this id is now empty", which is what a
local cache purges against.

### Host ASGI assembly (`routing.py`)

One call. `build_websocket_application()` discovers every installed app's
`routing.websocket_urlpatterns`, so nothing here is named:

```python
# asgi.py — the whole file
from django.core.asgi import get_asgi_application
from stapel_realtime.asgi import build_websocket_application

application = build_websocket_application(http_application=get_asgi_application())
```

The hand-written `ProtocolTypeRouter` this module used to document is gone.
Three hand-written ASGI files are why the fleet had three auth stacks and three
close-code sets — and in the deployment that prompted 0.3.0, an origin guard
nobody had put in front of a socket that authenticates by cookie.

## Anti-patterns

- **Don't ship a polling fallback.** Not in a client, not behind a flag, not
  "just for hosts without Redis". A fallback a product can end up in silently is
  how this module shipped a chat that refreshed every few seconds while its
  websockets were "done". If the socket cannot run, the deployment is broken and
  says so at boot.
- **Don't replay by `seq`.** Anchor on `rev_seq`. Filtering on `seq` silently
  drops every edit and every tombstone that happened while a client was away —
  and it looks completely correct in a test where nobody goes offline.
- **Don't sort by the envelope's `seq`.** That is a journal cursor; an edited
  message would jump to the end of the thread. Sort by the payload's `seq`.
- **Don't order messages by timestamp** either. `seq` is the total order; two
  messages in the same millisecond still have a definite order and a stable
  anchor.
- **Don't `DELETE` a message row.** A tombstone is the only delete that reaches
  the machines still displaying the message. A removed row erases it on the
  server and nowhere else, and tears a hole in a sequence the protocol assumes
  is gapless. This applies to erasure too — see `services.erase_user_messages`.
- **Don't allocate a sequence outside the locked path.** `post_message`,
  `edit_message`, `delete_message` and `erase_user_messages` all take it under
  `select_for_update`. Hand-inserting a `Message` with a chosen `seq` bypasses
  the counter, emits nothing, and reaches no open socket.
- **Don't give two rows the same `rev_seq`.** The socket deduplicates by seq, so
  a shared one delivers the first frame and swallows the rest — the bug
  `erase_user_messages` reserves a contiguous block to avoid.
- **Don't store files here.** An attachment is an opaque CDN key plus render
  metadata; the module never sees bytes.
- **Don't compute render metadata here.** Transcoding a 16px webp, measuring an
  aspect or drawing a waveform is `stapel-cdn`'s job, asked for by comm. A second
  implementation is a second answer to "how big is this picture".
- **Don't close the registries.** A new attachment kind or activity state is a
  settings line. Turning either into an enum means the sticker work reopens this
  contract.
- **Don't put content on a wide stream.** `chat:conv:<id>` and `chat:user:<id>`
  may both carry a message body because on both the `authorize()` gate equals the
  right to read that body. A workspace-wide stream would not.
- **Don't add a draft/compose concept.** Enter sends. A server-side draft puts a
  round trip between the key and the message.
- **Don't depend on realtime for correctness.** Durable facts replay by
  `rev_seq`; ephemeral ones are recoverable by refetch. Both remain true.
- **Don't emit outside the transaction.** Use `mutate_and_emit()` so the row and
  its event commit together (the emit-check gate enforces this).
- **Don't hand-write `asgi.py`.** `build_websocket_application()` — see above.
- **Don't import other stapel modules** — cross-module is comm by string name.
  `stapel_realtime` is the exception and not a peer: it is the substrate whose
  consumers this module subclasses, the way every socket in the fleet does.
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
