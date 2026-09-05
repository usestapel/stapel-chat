# Changelog

All notable changes to stapel-chat are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.1] — 2026-09-05

### Added — `chat.post_system_message`: a sibling service can record what happened

A thread is where two people agreed something, and things that happen outside
chat belong in it — a call ended, a booking was confirmed, an order shipped.
Until now a sibling module had exactly two ways to write that line: import
`stapel_chat.services`, which this shelf does not permit, or go through the
REST endpoint as a user, which is not what it is. So most of them wrote
nothing, and the thread quietly stopped being the record it is supposed to be.

The Function is **narrow, and the narrowness is the whole design**. Its
payload is `{conversation_id, body, client_msg_id?}` and nothing else:
`sender=None` and `kind=system` are hard-coded in
`services.post_system_message`, `additionalProperties: false` is on the
schema, and there is no argument anywhere in the path that names an author. A
general `chat.post_message` on the bus would be a way to put words in a user's
mouth — in a product where a conversation is evidence about a deal — and the
distance between "we need to write a line" and "anything on the bus can
impersonate anybody" is one convenience parameter.

Three smaller decisions, each of which had a wrong-looking obvious answer:

- **The body is a marker key, not a sentence.** `video.call.ended:188`, the
  convention this module's own `chat.support.assigned` already uses. A
  rendered string freezes one language and one number format into a row that
  outlives both, and the reader's locale is not the writer's. (Known gap: a
  `Message` has no structured-parameter field, so an argument rides after a
  colon. The marker keeps its meaning if such a field arrives.)
- **An unknown conversation raises `ConversationNotFound`** — a `LookupError`,
  matching `MessageNotFound` and for the same reason. A caller on the far end
  of a bus has to tell "the thing you named is gone, stop retrying" from "I
  could not answer, retry". A quiet no-op would make a service that lost its
  thread reference look exactly like one whose writes are landing.
- **Block rules are not consulted.** There is no sender for a block to be
  about, and a system line is a statement of what happened rather than
  somebody reaching somebody. Two people who blocked each other still see that
  their call was declined.

`client_msg_id` is the existing per-conversation idempotency key, so a caller
on an at-least-once transport passes one derived from the fact it is recording
and a redelivery writes one line, not two.

### Notes

- The first WRITE on this module's comm surface; the other two Functions stay
  reads.
- `llms.txt` budget raised 5800 → 6000, deliberately, with the argument in the
  Makefile: the line that cannot be cut is the one saying why the Function is
  shaped this narrowly, because an agent that reads only "posts a system
  message" writes the general one it was reaching for.
- Patch, not minor: additive, nothing existing changes shape.

## [0.8.0] — 2026-09-04

### Presence is told to accounts, not to sessions

Found on a live stand: an anonymous visitor taps "message the seller", the
storefront mints a guest, and the room that opens carries the seller's
`last_seen_at` and `online_until` — "last seen 38 minutes ago" handed to
somebody who has not named themselves, one tap at a time and once per
throwaway browser profile.

Nothing was bypassed. A guest is a stored user; it passes `IsAuthenticated`
like everybody else, which is the point of guests — the storefront's whole
guest wall is "an account is minted at the moment of the action". The wrong
part was that this module asked `is_authenticated` when the question it
needed answered was **is there a person with an account behind this
session**. Those are two different questions and only one of them is about
consent.

Both paths now ask the second one (`presence.is_account`):

- **REST** — `services.presence_for(conversations, viewer=…)` answers a guest
  with an empty map, so every participant ships the offline default the DTO
  already documents. Every HTTP path passes its `request.user`; a server-side
  caller that passes no viewer reads unfiltered, as before.
- **The live flip** — `presence.announce` skips a conversation a guest takes
  part in. The frame is addressed to a stream, not to a person, so there is
  no per-recipient filter at delivery: the choice is between telling the
  guest and not sending it.

The account on the other side of the thread loses nothing over REST — it is
the guest's session that is answered short, not the seller's. A deployment
whose user model has no guests is unaffected: the field this reads does not
exist there and the test is exactly `is_authenticated` again.

`PRESENCE_REQUIRES_ACCOUNT=False` restores the old answer as something a
deployment states rather than inherits.

Minor, not patch: a guest that used to read a counterpart's presence now
reads the offline default. That is the fix, and a client rendering the
default is a client already rendering "last seen — unknown".

## 0.7.4

- CONFIG.MD: `ATTACHMENT_TYPES`/`ACTIVITY_STATES` declare source `env` (the two-token contract has no `settings`); the 0.7.3 wheel shipped the invalid rows and broke `assemble_scaffold` for every selection including chat.

## [0.7.3] — 2026-08-30

### An "online" nobody ever takes back

Found on a live stand, not in a test: a reader's header said the other person
was online ninety seconds after they were gone — while the server had already
said offline. Nothing in the server was broken. The lease had expired exactly
as 0.7.0 designed it, and that is the whole problem.

`chat.presence.changed` is announced from a **disconnect**. A lease running
out announces nothing, because nothing happens: no socket closes, no row is
written, there is no event to send. And the lease exists precisely for the
case where no disconnect ever runs — a killed tab, a lost worker, the socket
that dies without telling anybody. So exactly when the counter cannot help,
the server heals silently on its own clock and every subscribed client keeps
rendering the last thing it was told, forever.

`online_until` now rides on `ParticipantResponse` and in the
`chat.presence.changed` payload. It is the deadline the server itself
evaluates, handed to the reader so the reader reaches the same answer alone —
**a fix by data rather than by event**. An `online` frame is now
self-limiting: it says how long it is good for, so a client that never hears
another frame still expires it. No poll, no extra traffic, and no invented
event for a non-happening.

Additive: the field is absent from the schema's `required` list, like `online`
and `last_seen_at` before it, so a client on the 0.7.0 contract reads a 0.7.3
body unchanged and simply keeps its old behaviour.

The pinned case is the one the stand produced — `connections > 0` because the
disconnect never ran, `online_until` in the past — and the assertion is both
halves: the body says offline, and it carries the deadline that makes that
answer checkable by the client on its own.

## [0.7.2] — 2026-08-30

### Fixed — the poison-pill guard needed a floor on stapel-moderation

0.7.1's fix is correct and unchanged; its new test is not self-contained. This
suite installs stapel-moderation for real (a fake would assert nothing but this
suite's own idea of the other side), and that module subscribes to
`user.deleted` too. On <= 0.3.0 its handler raised `ValidationError` on the
malformed id, so the test failed on the sibling's bug rather than passing on
ours. The test-extra floor is now `stapel-moderation>=0.3.1`, which carries the
same fix. 0.7.1 was never published.


## [0.7.1] — 2026-08-30

### Fixed — a malformed id in an action payload was a poison pill

`ValidationError` is not a `ValueError`. Django answers a key it cannot coerce
to a column's type — a malformed UUID above all — with
`django.core.exceptions.ValidationError`, which does **not** subclass
`ValueError` or `TypeError`. The `user.deleted` / `user.merged` guards here
caught only `(ValueError, TypeError)`, so a bad id walked straight through
them, the handler raised, `consume_actions` re-raised to the bus, and the
event came back forever: a redelivery loop over a payload no retry can repair,
burning the consumer's retry budget while looking exactly like a downstream
outage.

The consumed contracts do not save anyone from this. They type an id as
`{"type": "string"}` — and where they do say `format: uuid`, `jsonschema`
does not enforce `format` unless a format checker is passed, which the comm
registry does not do. A malformed id is a well-formed payload.

`handle_user_deleted` (previously unguarded — the call went straight at
`ChatGDPRProvider`) and `handle_user_merged` now catch `ValidationError`
alongside `ValueError`/`TypeError` and take the same quiet path they always
took for an id they have never seen.

`user.merged` had a second door: the *from* id was probed under the guard but
the survivor probe, `get_user_model().objects.filter(pk=into_user_id)`, sat
outside it, so a malformed *into* id still escaped whenever the guest genuinely
owned chat. That read moved inside the guarded block — still before the first
write, so the "survivor not projected yet" path can no more leave a thread
half-moved than it could before.

`MergeTargetNotReady` is untouched: a survivor id that *parses* but has no row
here still raises, because that one is a real ordering lag and redelivery does
fix it.


## [0.7.0] — 2026-08-30

### "На связи" was never about the other person

The thread header said *"На связи"* — "connected" — whenever **the reader's
own** websocket was up. In a classified marketplace it sat next to the
seller's name and it read as *"the seller is online"*. It was never that. It
was the browser reporting that it could still reach the server, and two people
sitting in a dead thread were each told the other one was there.

Presence is now a server-side fact about a **user**, derived from that user's
own connections (`presence.py`, `UserPresence`):

- `participants[].online` and `participants[].last_seen_at` on every
  conversation body, so a header paints correctly on first load with no extra
  round trip and nothing to derive;
- `chat.presence.changed` on the conversation streams the user takes part in —
  the stream a thread is already subscribed to, so presence costs no second
  subscription;
- `PRESENCE_TTL_S` / `PRESENCE_WRITE_THROTTLE_S` / `PRESENCE_FANOUT_LIMIT`.

**Online is the AND of a connection count and a lease.** The counter makes the
last tab closing visible to the peer at once; the lease is the part a worker
killed mid-socket cannot leave behind, because a counter that never decrements
would keep that user online forever. Whichever fact is wrong, the answer
degrades to *offline* — a false "online" is the whole defect, so that is the
direction this has to fail in. A disconnect whose connect was never recorded
floors the counter at zero rather than driving it negative, which would
otherwise strand a user offline while a real tab of theirs was open.

**A heartbeat is evidence of life, not a write.** Every inbound frame touches
presence — the substrate's `pong` included, which is the cheapest liveness
signal there is — but `touch()` writes at most once per
`PRESENCE_WRITE_THROTTLE_S`, in one conditional UPDATE that needs no read, and
each socket holds a second in-process guard so a throttled touch does not even
cost a thread hop. `stapel_chat.E021` refuses to boot a deployment whose
throttle is not below its TTL: a healthy socket would let its own lease lapse
between two permitted writes, and the peer would watch somebody sitting right
there blink offline on a timer.

Only flips are announced. A renewal that changes nothing tells nobody
anything, and fanning one out would put a signal on every one of a user's
threads every few seconds.

**The client's own transport health is untouched.** `stapel-realtime`'s
degradation states are the honest indicator for "my connection", and they stay
exactly where they were. What is gone is one control answering both questions.

No backfill: a user nobody has seen connect is offline with no last-seen, and
the read path says exactly that. Stamping "now" on everyone at migration time
would tell every peer that everybody had just been here — a worse lie than the
one this release deletes.

## [0.6.3] — 2026-08-28

### A merge deleted the guest and their conversations went with it

`user.merged` arrives when an anonymous visitor signs into a real account.
This package now consumes it and moves the guest's participations, authored
messages and any `assigned_operator` rows onto the survivor.

Nothing consumed it before, so the guest row was deleted and every CASCADE
foreign key died with it: a visitor who wrote to a seller and then signed in
lost the conversation, silently.

The consumer decides inside a transaction before any write, and the order is
deliberate: a guest that owns nothing here returns quietly — which also covers
the second delivery of an already-completed merge, so idempotency stays silent
— and only a guest that owns something and a survivor with no local row yet
raises, so the outbox redelivers rather than tearing the transfer in half.
A test asserts the rows are still under the guest after such a raise, not
merely that it raised.

`direct_key` is recomputed for a moved thread, and a test pins that it is
unchanged when the raise happens.

## [0.6.2] - 2026-08-28

### Fixed — `E017` now says what it cannot see

The boot check that fires when `BLOCK_ENFORCEMENT` is `required` and the
provider is unreachable told an operator how to fix a *local* mount and
nothing about the case this fleet actually deploys. Over a bus transport
`function_unreachable_reason` cannot prove a remote provider is up — the
subject IS the function name and there is no route table — so a green boot is
not evidence, and a profiles service one release too old surfaces only at the
first call. stapel-classified's own `E002` said that out loud and this one did
not; the sentence is ported before that check is deleted, so the honesty is
not lost with it.

The hint now also names the floor explicitly — **`stapel-profiles >= 0.16.0`**
is the first release serving `profiles.relationships` — instead of naming the
package with no version. Text only: no behaviour, no signature, no id changes.

### The promised deletion happened

0.6.1 said stapel-classified's pre-creation block door "is deleted by its next
patch now that this has shipped". It was: **stapel-classified 0.4.0** removes
its `blocks.py`, its `BLOCK_ENFORCEMENT` / `BLOCK_FUNCTION` keys, its
`E001`/`E002`/`W001`/`W002`, its `ContactRefused`, and the error keys
`error.403.classified_contact_refused` /
`error.503.classified_blocks_unavailable`. Both write doors are this module's
alone now, and the composite's whole statement about blocking is the value
`required` it sets on `STAPEL_CHAT["BLOCK_ENFORCEMENT"]` in its preset — one
fact, one axis. That release floors itself at `stapel-chat>=0.6.1`, because
against 0.6.0 the deletion would re-open the door.

## [0.6.1] - 2026-08-26

### SECURITY — a block now refuses the thread, not just the message

**A behaviour tightening on an existing verb: `create_direct` can now refuse.**
Nothing about its signature or its return changed, and no caller has to change
anything — but a call that always succeeded before can now raise, so read this
before upgrading.

0.6.0 enforced blocks at **send**. The consequence was a door left ajar: a
blocked buyer could still open a thread with a seller who had blocked them,
type a message, press Enter and only *then* be refused. Composites that wanted
that shut earlier had to keep a pre-creation block check of their own —
stapel-classified's "contact the seller" door existed partly for this, is
recorded there as ask #5, and **is deleted by its next patch now that this has
shipped.** That deletion is the consumer-visible point of this release.

`create_direct` consults `STAPEL_CHAT["BLOCK_FUNCTION"]` (default
`profiles.relationships`, asked by name, never imported) before creating a new
direct conversation, and refuses if the pair is blocked in either direction.

### The distinction this release is actually about — create vs return

`create_direct` is idempotent: it returns the pair's existing thread. Those
two branches are **not** the same act and are not treated the same.

| Act | Blocked pair | Why |
|---|---|---|
| opening a thread that does not exist | **refused** | it is a write, and it is the door the ask exists to close |
| returning a thread that already exists | **allowed** | it is a READ of history, and across this fleet **a block never deletes history** |

Both parties keep seeing everything already said to each other; neither can add
to it, because the send enforcement from 0.6.0 still refuses. What a client
renders for a blocked pair with history is an open thread with no composer.

Collapsing the two rows in either direction is a defect, and both directions
are asserted rather than trusted: refuse-always would take a conversation off
two people as a side effect of one of them tapping "block", and allow-always
would reopen the door. `tests/test_blocks.py::TestReturningAnExistingThread`
fails a refuse-always implementation; `TestCreatingANewThread` fails 0.6.0.

A property that follows from the ordering — the existence lookup runs first and
the provider is asked only on the create branch — is that **no outage of the
block store can ever stand between somebody and their own correspondence.**

### One refusal vocabulary, not two

The creation refusal **is** the send refusal: the same `services.SendRefused`,
the same 403 `error.403.chat_send_refused`, a key that names no block, no
direction, no blocker identity. A second key for the creation door would itself
be a disclosure — a client that could tell "refused to open" from "refused to
send" could tell a block from a coincidence, which is the whole thing
non-disclosure exists to prevent.

### The availability posture is unchanged, and now covers both doors

A provider that is present and **failing** answers 503
(`error.503.chat_blocks_unavailable`), never a created thread.
`BlockCheckUnavailable` remains deliberately **not** a `ChatError`, so a 503
can never be caught as a 403 — asserted at the new door too. An outage is not
consent, and it does not open a thread either.

`BLOCK_ENFORCEMENT` keeps its default of **`auto`**, for 0.6.0's reason
unchanged: a generic messaging library deployed without stapel-profiles must
not 503 on every conversation. A composite that knows it has blocks sets
`required` itself, as stapel-classified does. `off` is still a decision on the
record — its `W004` text now says plainly that *both* doors are unlocked, since
nothing else in the fleet is covering for that setting any more.

### Unchanged

- **Group and support threads are not checked**, at creation any more than at
  send. A group room is somebody else's convening; an operator is not a peer,
  and a customer must not be able to mute the help desk.
- **The send path.** No change to `post_message`; its tests are untouched and
  still green.
- **`chat.conversation.created`** is still emitted only on a real create — a
  refused create emits nothing, and a returned thread never did.
- **stapel-core floor stays `>=0.45.0`.** 0.46.0 adds the `verification` drop
  verbs (`drop_challenge`, `drop_verification_token`, `revoke_grants`); chat
  uses none of them, and nothing this module relies on moved.

## [0.6.0] - 2026-08-24

### ⚠️ BREAKING — a direct thread's identity now includes what it is ABOUT

**Read this before upgrading: it changes what `create_direct` returns.**

Through 0.5.x a direct conversation was keyed by an order-independent hash of
the participant PAIR and uniquely constrained on it. One buyer and one seller
could therefore hold **exactly one thread, forever**, however many things they
discussed. A buyer asking about a second listing landed in the conversation
about the first — under the first one's header.

Nobody could work around that correctly, and the module that tried says so
plainly. stapel-classified could not refuse the second listing, *because
refusing it would have rendered the wrong card*, so it made its own
conversation binding **append-only with several subjects per conversation**
and picked which to show. That table exists only because of this defect and is
marked for deletion now that this has shipped.

`direct_key` is now computed over `(scope, {both user ids}, subject_type,
subject_key)`.

**What happens to threads that already exist — the important part.**

- **Nothing is migrated, and nothing is lost.** The subject segments are
  appended to the key *only when there is a subject*, so a subject-less key is
  **byte-identical** to the one 0.5.x produced. Every conversation that exists
  today keeps its id, its messages, its participants and its key, and an
  unchanged `create_direct(owner=…, other_user_id=…)` still returns it.
  `tests/test_subjects.py::test_a_subjectless_key_is_byte_identical_to_the_old_one`
  is that guarantee, asserted rather than trusted — had it drifted by one
  byte, the module would have opened a silent second thread beside every live
  one.
- **An existing thread is the pair's "about nothing in particular" thread.**
  That is now a real, permanent category, not a placeholder.
- **The first subject-bearing contact after the upgrade opens a NEW thread**
  rather than adopting the pair's existing one. This is deliberate: the old
  thread genuinely is not about that subject, and quietly relabelling it would
  put a listing's header over a conversation that predates it. A deployment
  that would rather adopt an existing thread must decide *which* subject it
  was about — a question only that product can answer, which is exactly why
  this module does not answer it. Expect users with existing correspondents to
  see one additional thread appear the first time a subject is used.
- Migration `0004` is expand-only: two blank-defaulted columns, one widened
  `CharField` (`direct_key` 600 → 900), one index. No backfill, no rewrite, no
  drop.

### Added — subjects: what a conversation is about, with the card inlined

- **`subject_type` / `subject_key` on a conversation** — an opaque pair, never
  parsed here. It is moderation's `(target_type, target_key)` idiom for the
  same reason: the pair is a NAME, and a messaging engine that learned what a
  listing is would be the wrong place for that knowledge.
- **`STAPEL_CHAT["SUBJECT_TYPES"]`** — merge-over-builtins registry
  (`subjects.py`), **shipping EMPTY**. A generic chat has no subject types,
  and the obvious one (`listing`) belongs to whoever owns listings. Settings
  ← runtime, `None` removes, same semantics as the attachment and activity
  registries. Each policy names a **`card_function`**; a policy without one is
  refused at registration and `stapel_chat.E020` at boot.
- **Cards are batched, and designed to `classified.subject_cards`** —
  `{keys: [...]} → {cards: {key: card}}`, one call per subject type for a
  whole page, never one per conversation. The card is passed through
  untouched. A provider that answers a deleted subject with a `gone` card
  (which classified's contract requires) simply works; a provider that
  *omits* a key it was asked about is reported as a degraded card, because
  rendering that as "no subject" would hide a broken provider behind a
  plausible header.
- **Degradation is data**: every subject carries `meta_status` /
  `meta_reason` (`subject_type_unregistered`, `card_function_unreachable`,
  `card_function_failed`, `card_missing`) in the attachment vocabulary. A
  conversation never fails to open because a catalogue blinked.
- Creating a conversation with an **unregistered** subject type is refused
  (400 `chat_unknown_subject_type`), and half a subject — a type without a key
  or vice versa — is refused too (400 `chat_incomplete_subject`).

### Added — `chat.conversation.created`

A thread was opened. Written into the outbox in the same transaction as the
row, and emitted **only on a real create** — an idempotent `create_direct`
that returned an existing thread is not one, and a consumer that bound a
domain object per idempotent retry would be right to double-bind.

Before this, nothing downstream could react to a new conversation at all: a
consumer learned one existed when its first message arrived on `chat.message`.
That is the entire reason stapel-classified's binding was **client-driven** —
a client telling the server what had happened, in a fleet that is otherwise
server-authoritative about exactly this.

### Added — `chat.conversation_participants`

Who is a party to these conversations, batched, with each thread's kind and
subject. stapel-classified stored `initiator_id`/`counterparty_id` on its own
row **only** because chat exposed no way to ask; a copy nothing can refresh
goes stale the moment a participant changes here.

Every id supplied is answered, including one that names nothing
(`{"exists": false}`) or is not a well-formed id at all — the same rule
`classified.subject_cards` follows for a deleted listing: a caller holding a
dead id must be told, not left to infer it from an absence. Deliberately **not**
a permission check: it answers who is a party, and what that entitles them to
is the caller's policy.

### SECURITY — a block now stops a send, not just a new conversation

A block that only refuses NEW conversations is **half a block**: nothing
stopped the next message in a thread that already existed. Enforced in the
service layer, so it covers the socket — the canonical send path since 0.3.0 —
and not only REST.

The provider is asked **by name, never imported**:
`STAPEL_CHAT["BLOCK_FUNCTION"]`, default `profiles.relationships`
(stapel-profiles), `{"pairs": [[a, b], …]} → {"blocked": [[a, b], …]}`, either
direction. Chat and the block owner stay independently deployable, and this
shipped without waiting on that module.

- **The refusal discloses nothing.** 403 `error.403.chat_send_refused` — a key
  that does not name a block, from an exception that carries no reason and no
  direction and must never grow either. Telling the blocked party "they
  blocked you" turns a quiet boundary into a notification.
- **A provider that is present and FAILING answers 503, never "allowed".**
  `BlockCheckUnavailable` → `error.503.chat_blocks_unavailable`, deliberately
  *not* a `ChatError`, so a 503 can never be caught as a 403. This is
  stapel-classified's precedent kept identical, so the two modules cannot
  disagree about what an unreachable block store means. An outage is not
  consent; failing open would deliver a message to somebody who blocked the
  sender, and they would never know.
- **`BLOCK_ENFORCEMENT`** — `auto` (enforce when a provider is reachable;
  `W003` says so at every boot when none is), `required` (an unreachable
  provider is `E017` at check time and 503 at send), `off` (a decision on the
  record, `W004`). "Blocks are not enforced here" is a sentence an operator
  reads, never something they discover.
- **Direct threads only.** A group room is somebody else's convening and
  silently dropping one member's messages out of it is a different product; a
  support thread is never checked, because an operator is not a peer and a
  customer must not be able to mute the help desk by blocking an agent. Both
  exclusions are asserted by tests so neither can drift into an accident.

### Changed

- **Floor: `stapel-core>=0.45.0`** (was 0.43.0).
- New checks: `E017`/`W003`/`W004` (block enforcement), `E018` (an
  unrecognized enforcement mode — not a thing to leave implicit for a security
  control), `E020`/`W005` (subject types and their card functions).
- `ConversationResponse` gains `subject` (null on a thread about nothing in
  particular); `CreateConversationRequest` gains `subject_type`/`subject_key`.
  Both are additive — an existing client keeps working unchanged.

## [0.5.1] - 2026-08-24

*(0.5.0 was tagged and never reached PyPI: its moderation-seam tests imported
`stapel_moderation` from an autouse fixture, which errored **every** test in
the file at setup on a clean runner — `ModuleNotFoundError`, green here only
because the shared development virtualenv has the whole fleet installed. The
release gate did its job. **0.5.0 does not exist on PyPI — floor on
`>=0.5.1`**; this release is 0.5.0's feature set unchanged, plus the gate
below. Third instance of this class in one night, after stapel-core 0.44.0 and
a stapel-tools nav-manifest test, which is why the fix is a mechanism rather
than an import.)*

### Fixed — the test suite now declares what it needs, and it is checkable

**`[project.optional-dependencies].test`** is new: the siblings this suite
imports, named in one place, installed by CI as `pip install -e ".[test]"`
instead of a hand-kept package list in a workflow file that no test could
disagree with. `stapel-moderation` and `stapel-cdn` are in it.

**`tests/test_test_dependencies.py`** is the gate. It parses every file of the
suite — including `conftest.py`, and including imports nested inside
functions, fixtures and `try` blocks, which is where both of tonight's hid —
collects the `stapel_*` packages they import, and fails if one is declared
neither as a runtime dependency nor in the `test` extra. "It works in my venv"
stops being a dependency declaration; `pyproject.toml` is. A second test walks
the other direction so the extra cannot rot into a wish list.

**The quieter half of the same defect is also closed.** 0.4.0 added two tests
asserting that chat's attachment types and stapel-cdn's `BUILTIN_MEDIA_KINDS`
are the same set — "asserted rather than agreed by comment". They were wrapped
in `except ImportError: pytest.skip(...)`, stapel-cdn was installed on no CI
runner, and so the agreement was enforced **nowhere** while the changelog said
it was enforced. A skip that can never not happen is not a test. stapel-cdn is
declared now and both tests run.

**`STAPEL_TEST_STRICT_SIBLINGS=1`** (set by both workflows) turns a missing
declared sibling from a skip into a failure. On CI the extra is installed, so
a skip there means the install step did not do what the workflow says — and
that would be exactly this bug wearing its other face.

### Changed — which tests meet the real module, and which do not

The split in `tests/test_moderation_seam.py` is deliberate and is the rule
worth reusing: **a test that claims to prove interop is never faked, and a
test that proves only this module's half must not need the sibling
installed.**

- Chat's own half — the `chat.moderation_content` Function, the tombstone and
  erasure rules, the composite `<conversation_id>:<message_id>` key — needs no
  moderation queue at all. It never did; only the autouse fixture did. That
  fixture is now requested by name, by the tests that actually want it.
- The interop half — registering the target type and reading a message back
  through `stapel_moderation.services.fetch_content`, the host-declaration
  precedence, the reachability of the registered `content_function` — still
  runs against the real stapel-moderation, which is now installed on CI. A
  registered fake there would assert nothing but this suite's own idea of the
  other side of the seam.

No library code changed in this release. `stapel_chat` 0.5.1 is byte-for-byte
0.5.0 in everything a host imports.

## [0.5.0] - 2026-08-24

### Added — a message can be reported, and the report is about the message

stapel-moderation is target-generic: its target registry ships EMPTY and it
learns what a "chat message" is from whoever knows. Nobody did. The only way
to complain about a message anywhere in the fleet was stapel-classified's
**evidence-based** `chat_message` policy — the reporter's own screenshot,
carried in the report and stamped unverified, because no module served a
message's content. This module stores every message it delivers, so that was
never the truth; it was a gap wearing a workaround.

- **`chat.moderation_content`** (comm Function, `schemas/functions/`) — one
  message's live content for an external moderation module: body, attachment
  KEYS, author, and the conversation it came from. Fetched when it is looked
  at, so a case opened hours ago shows the message as it is now, edits
  included. `services.moderation_content` is the same call in-process.
- **`stapel_chat.moderation`** — `MESSAGE_TARGET_POLICY` (the `chat_message`
  policy: `gate: "post"`, no intake topic, `id_field: "message_id"`,
  `verdict_event: None`, `media: False`, the universal taxonomy minus the
  codes that describe goods) and `register_moderation_target()`, called from
  `apps.ready()`.
- **`MODERATION_TARGET_TYPE`** (default `"chat_message"`, `""` disables) — a
  config axis, curated in `docs/capabilities.meta.json`.

**Registered only into a gap.** stapel-moderation's runtime registry layer
outranks settings, so registering unconditionally would silently overwrite a
composite's deliberate policy — stapel-classified declares this very type.
A host declaration always wins; ours fills a hole. Without stapel-moderation
installed nothing here runs: it is not a dependency in either direction.

**A tombstone is gone, not empty.** A deleted or GDPR-erased message has an
empty body by construction, and handing that back would show a moderator a
blank card indistinguishable from a message that said nothing. The new
`services.MessageNotFound` (a `LookupError` — the `*.moderation_content`
family's documented contract) makes moderation answer `target_not_found`
instead, which is also what stops a moderation case from becoming the one
place erased text survives.

There is deliberately **no verdict consumer and no conversation target** — see
MODULE.md §7 for why each is a statement rather than an omission.

### Changed

- `_codegen_settings.settings_kwargs(extra_apps=…)` — test-harness only, so
  the seam is exercised against the real queue instead of a stand-in. The
  contract harness never passes it: a co-mounted module would put its error
  keys in this module's emitted catalogue.

## [0.4.0] - 2026-08-24

### ⚠️ BREAKING — chat and the CDN now speak one vocabulary instead of two

**stapel-cdn 0.16.0 shipped the metadata half of the attachment contract this
module defined in 0.3.x**, and it named some of the same things differently.
Rather than translate between the two, chat adopts the CDN's names. Two names
for one thing, kept in step by comment, is the exact class of seam defect this
fleet keeps paying for — and it is much cheaper to close it now, with one
consumer, than after a frontend has been built against both.

| 0.3.x | 0.4.0 | why |
|---|---|---|
| type `voice` | type **`audio`** | the CDN's media kind for it. A voice-note bubble is a render choice; `audio` is what the thing *is* |
| `waveform_b64` | **`preview_b64` + `preview_kind: "waveform"`** | one preview slot, one discriminator — see below |
| — | `preview_kind`, `poster_url`, `square`, `animated`, `meta_status`, `meta_reason` | new, all from the CDN |

Two tests now assert the agreement rather than a comment: chat's builtin
attachment types and the CDN's `BUILTIN_MEDIA_KINDS` must be the same set, and
each type's `preview_kind` must equal the CDN's `preview`. If they ever drift
again, the suite fails.

### Changed — the preview is a pair, on purpose

`preview_b64` is the bytes; `preview_kind` says what they depict (`blur` /
`poster` / `waveform` / `null`). They are two fields and not one nullable field
because **`preview_kind` follows from the type, so it is known before any
preview exists** — a client can reserve a waveform-shaped box for a voice note
whose waveform the CDN is still rendering. That is the whole point of not
jumping the layout, and collapsing the pair throws it away. The client does not
get to assert `preview_kind` either; the registry decides it.

### Changed — one comm call per message, not one per attachment

Enrichment moved from `cdn.describe` (one ref) to **`cdn.describe_many`** (a
page), which resolves in one query per model. A ten-attachment message costs
one round trip instead of ten. Batches are paged at the CDN's limit of **50
refs per call**, because every snapshot may inline a preview and so the batch
size *is* the response size.

### Changed — degradation is data, and so is a dead ref

- **`meta_status` / `meta_reason` travel with every attachment.** A degraded
  attachment stays renderable, and the reason is named (`ffmpeg_missing`,
  `not_generated`, `preview_over_budget`, `unknown_ref`) so a client can tell
  "still generating" from "this deployment has no ffmpeg" and draw the right
  placeholder for each. An attachment nothing has described yet says so —
  `missing` / `not_described` — rather than presenting unexplained nulls.
- **An unresolvable ref comes back as data, not an exception**, mirroring
  `describe_many`: a message with one dead attachment still renders the other
  nine, and that one carries `meta_reason: "unknown_ref"`.
- **`duration_ms: null` from the CDN now overwrites a client's guess.** `null`
  means *unmeasured* and never zero — a zero-length voice message and an
  unmeasured one are different facts a UI draws differently — so a sender's
  optimistic number must not survive the authority saying it does not know.
  (`preview_b64`, `preview_kind` and `poster_url` are authoritative-null the
  same way; everything else still falls back to the client's value when the
  CDN is silent, which is what keeps a send working during an outage.)

### Changed

- **`MAX_PREVIEW_B64_BYTES` default 8192 → 4096**, matching stapel-cdn's
  `MICRO_PREVIEW_MAX_BYTES` and measured the same way (on the finished `data:`
  URI). A larger number here would have accepted what the authority already
  refused. With `MAX_ATTACHMENTS` at 10 that bounds previews at ~40 KB per
  message; the CDN enforces its own budget by downgrade-then-refuse, never
  truncation, so an over-budget preview arrives as `null` plus a reason rather
  than as a broken image.
- **`stapel-core` floor `>=0.43.0`** (was `0.41.0`) — the floor stapel-cdn
  0.16.0 already stands on. The canonical `SerializerSeamMixin` was adopted in
  0.3.0 and the local copy is already gone.

### Migration

Stored attachments from 0.3.x keep working — `type: "voice"` simply is not a
registered type any more, so a host that shipped 0.3.x voice messages either
re-registers it (`STAPEL_CHAT = {"ATTACHMENT_TYPES": {"voice": {...}}}`) or
rewrites those rows to `audio`. Given 0.3.1 was on PyPI for under an hour and
0.3.0 never published at all, no data migration ships for it: inventing one
would be ceremony for rows that do not exist.

**Tests:** 164 pass (155 on 0.3.1) — the nine new ones cover the batch call, the shared vocabulary, the authoritative null and the named degradation.

## [0.3.1] - 2026-08-24

### Fixed — 0.3.0's tag could not be installed

The `v0.3.0` tag never reached PyPI. `stapel-realtime` 0.1.1 declared
`stapel-core>=0.33.2,<0.34` — the standard one-minor window, and the wrong
discipline for a substrate every socket-serving module builds on, because its
ceiling becomes theirs. 0.3.0 raised its core floor to `>=0.41.0` for the
canonical serializer seam, and the pair was unresolvable:

```
stapel-chat 0.3.0 depends on stapel-core<1.0 and >=0.41.0
stapel-realtime 0.1.1 depends on stapel-core<0.34 and >=0.33.2
ERROR: ResolutionImpossible
```

Fixed upstream in **stapel-realtime 0.1.2** (ceiling widened to `<1.0`; no code
change, its 193 tests pass unmodified against core 0.43 — the frame-type
equality test, not a version range, is what guards that seam). This release
raises the floor to `stapel-realtime>=0.1.2` accordingly.

0.3.1 is 0.3.0 plus that one-line floor. **Everything in the 0.3.0 entry below
describes this release** — read it as the upgrade note.

## [0.3.0] - 2026-08-24 — *tagged but never published; see 0.3.1*

### ⚠️ BREAKING — the WebSocket wire, the attachment shape, and the boot gate

**Read this before upgrading.** Three contracts changed at once and one new
class of deployment now fails `manage.py check` on purpose. Every one of them
exists because of the same finding, described below.

1. **The socket speaks the fleet's v1 envelope.** The flat frames of 0.2.x
   (`{"type": "message", "seq": 3, "body": "hi"}`) are gone. Every frame in
   both directions is now `{"v": 1, "type": ..., "stream": "chat:conv:<id>",
   "payload": {...}}`, with `seq` present on journal frames only. A 0.2.x
   client cannot read a 0.3.0 socket and vice versa.
2. **`attachments` is a list of objects, not a list of strings.** Migration
   `0003` converts existing rows; a bare ref string is still accepted on the
   way in.
3. **A deployment that cannot serve a socket no longer boots.** Five new
   ERROR-level checks (`stapel_chat.E010`–`E014`). There is no setting that
   turns them off, and that is the point — see *Why this is a boot error*.

### The finding

A live product's chat was opened, and Enter did not send. The panel said
"updates every few seconds". Websockets had been built, mounted, proxied and
smoke-tested; the product polled anyway, and nothing anywhere said so.

The root cause was not in this module. A browser cannot set an `Authorization`
header on `new WebSocket()`. The deployment authenticates HTTP with an httpOnly
JWT **cookie**; `stapel_core.django.jwt.channels` reads a token from the
Authorization header, the `Sec-WebSocket-Protocol` subprotocol or `?token=`,
and has no cookie branch. So every browser handshake closed **4401**, the
client read 4401 as a permanent refusal and stopped retrying, and the seam fell
through to its polling half — permanently, silently, and looking exactly like a
product decision. The one end-to-end proof that existed passed an
`Authorization` header a browser can never send.

That is a stapel-core fix and it is reported upstream. What this release owns
is everything that let the fallback stay invisible.

### Why this is a boot error and not a setting

A polling fallback that a product can end up in silently is itself the defect:
it is what made "websockets are done" a false claim. So realtime is not an
option a consumer wires — it is the module, and a deployment that cannot serve
it says so at boot instead of degrading into a timer. There is deliberately no
`REALTIME = False`. A knob would reproduce the defect, because the deployment
that shipped "updates every few seconds" never chose it either.

### Added — realtime, rebuilt on the substrate instead of beside it

- **`ChatConsumer` is now a `stapel_realtime.ResumableStreamConsumer`.** The
  hand-rolled socket is gone. `stapel-realtime`'s own module map named chat as
  one of the three duplicate WebSocket implementations it exists to end; this
  release stops being one. Chat supplies two journal hooks, the participation
  gate and its write frames. Authentication, the envelope, the heartbeat with
  token-expiry re-check, backpressure, revoke-to-kick, the close-code canon and
  the origin guard are all the substrate's, once.
- **`ChatInboxConsumer` — `ws/chat/inbox`.** A conversation list had no socket
  at all, by construction: one route existed and it was per-conversation, so
  the inbox refreshed on a 15-second timer no matter how live the open thread
  was. A chat that polls its inbox is a polling chat. The inbox stream is
  ephemeral (everything on it is recoverable by re-listing over REST) and its
  stream key is derived from the authenticated scope, so the route carries no
  user segment to tamper with.
- **Send, edit and delete over the socket** — `send` / `edit` / `delete` /
  `read` / `delivered` / `activity` frames, all through the same service layer
  the REST views call. One validation path, one emit, one fan-out.
- **`client_msg_id` — idempotent send.** Enter pressed once, socket dropped,
  client retried: one message, not two. Unique per `(conversation,
  client_msg_id)`, echoed back so an optimistic bubble reconciles with the real
  row. There is no draft concept anywhere in the contract, deliberately: a
  compose box is client state, and a save round trip between Enter and the
  message is the thing that must not exist.
- **Every conversation carries `stream_key` and `socket_path`.** A client never
  constructs a socket URL, and a UI that ignores the field is visibly ignoring
  something rather than quietly defaulting to a timer.

### Added — edit, and deletion as a tombstone

- **`PATCH /conversations/{id}/messages/{mid}`** sets the body, stamps
  `edited_at` and returns `edited: true`. Author only, optionally bounded by
  the new `EDIT_WINDOW_S`.
- **`DELETE /conversations/{id}/messages/{mid}` leaves a tombstone**, and
  answers `200` with the stripped row rather than `204`. The id keeps being
  delivered — by history, by replay, by this response — with `body: ""`,
  `attachments: []` and `deleted: true`, precisely so a client cache, a service
  worker or an offline database learns *which id to purge*. An id that stops
  arriving is an id nobody can purge; a row that vanished from the table would
  leave the copy on the client forever, which is the opposite of what a delete
  is for. (Same reasoning as core 0.40/0.41's deletion tombstone: the fact has
  to outlive every window in which somebody could still be holding the thing it
  invalidates.)
- **Retention of the tombstone is permanent.** Unlike a JWT tombstone there is
  no bounded credential lifetime to expire against — a client-side chat cache
  can be offline for months, so no TTL is safe, and the surviving row is an id,
  a sequence and two timestamps. It also keeps `seq` gapless and keeps
  `reply_to` resolvable. The single thing that removes content is erasure,
  which now *also* tombstones rather than deleting (below).
- **`Message.rev_seq` — a second sequence, and the reason edits work at all.**
  `seq` is the message's position in the thread: allocated once, immutable, the
  sort key and the history anchor. `rev_seq` is its position in the
  conversation's revision journal, re-allocated from the same counter on every
  edit and delete. Realtime replay is anchored on `rev_seq`, so a message
  edited or deleted while a client was offline arrives in the catch-up.
  Anchored on `seq` it never could: the row sits behind a cursor the client has
  already acknowledged. **A client upserts by `message_id`, orders by the
  payload's `seq`, and treats the envelope's `seq` purely as a resume cursor.**
- **Erasure publishes the destruction it performs.** The GDPR provider used to
  run `Message.objects.filter(sender_id=...).delete()`. That destroyed the
  content on the server and left every copy on every other participant's
  device, because nothing told those devices which ids had ceased to exist — and
  it tore holes in a sequence the whole protocol assumes is gapless. Erased
  messages now become anonymous tombstones with sequences reserved as one
  contiguous block per thread, and any socket the departing user still holds
  open is revoked before the row authorizing it disappears.

### Added — attachments that render on first paint

`attachments` moves from `["product/<hash>"]` to a list of descriptors carrying
everything a bubble needs to paint without a second round trip **and without
reflowing when the asset lands**:

| type | what it carries |
|---|---|
| `image`, `gif` | `aspect`, `bytes`, `preview_b64` (~16px webp data URI), `variants` |
| `video` | the above plus `duration_ms`, poster in `preview_b64` |
| `voice` | `duration_ms`, `waveform_b64` (a waveform **image**, so the client paints one `<img>`) |
| `file` | `mime`, `ext`, `name` |

- **The type set is an OPEN registry**, merge-over-builtins with `None`
  removing an entry: builtins ← `STAPEL_CHAT["ATTACHMENT_TYPES"]` ←
  `register_attachment_type()`. Stickers are already named as the next type;
  adding one is a settings line, not a reopened contract.
- **The metadata comes from `stapel-cdn` by comm, once, at send time** —
  `call("cdn.describe", {"ref": ...})`, whose answer is merged *over* the
  client's. Nothing is re-derived here: transcoding a 16px webp or drawing a
  waveform is the CDN's job, and a second implementation would be a second
  answer to "how big is this picture". An unreachable CDN leaves the client's
  values in place rather than failing the send.
- Inline previews are bounded (`MAX_PREVIEW_B64_BYTES`, default 8 KiB) and must
  be `data:image/...` URIs — they are untrusted bytes riding inside every
  message frame on their way to other people's screens.

### Added — receipts and activity states

- **Delivery and read receipts.** `ConversationParticipant.last_delivered_seq`
  joins `last_read_seq`; both move forward only, both are returned with the
  conversation, and both fan out `chat.delivered` / `chat.read` as ephemeral
  Signals when they move. Holding a message and having looked at it are two
  different facts, and only the client knows the difference — which is why
  delivery is an explicit call rather than something inferred from an open
  socket.
- **Activity states — another OPEN registry.** `typing`, `recording_audio`,
  `sending_video`, `uploading_file`, `idle` out of the box; "choosing a
  sticker" is a settings line. Each carries a `ttl_s` so an indicator expires
  on its own — the only design that survives a tab closed mid-word. Nothing is
  persisted.

### Added — the checks

| Id | Level | Fires when |
|---|---|---|
| `stapel_chat.E010` | error | `stapel_realtime` is not in INSTALLED_APPS |
| `stapel_chat.E011` | error | No `CHANNEL_LAYERS["default"]` — fan-out is a no-op and the product will poll |
| `stapel_chat.E012` | error | HTTP authenticates by cookie and the Channels middleware cannot read one — **the defect above, as a boot failure** |
| `stapel_chat.E013` | error | `SIGNAL_TRANSPORT` resolves to nothing — dead ticks, a dead inbox, and a journal that still works |
| `stapel_chat.E014` | error | Cookie auth with an empty `STAPEL_REALTIME["ALLOWED_ORIGINS"]` — a cookie is ambient authority, so an unguarded socket is CSWSH of a live conversation |
| `stapel_chat.E015/E016` | error | A malformed attachment-type / activity-state registry entry |

`E012` is a **behavioural probe**, not a version pin: it hands the core's
extractor a handshake carrying only a cookie and reads the verdict off the
result, so it stays true however the core reorganizes. A probe that cannot ask
never asserts.

### Changed

- **`stapel-core` floor `>=0.41.0`** (was `0.36.0`) — the canonical
  `SerializerSeamMixin` / `StapelAPIView` from 0.37.0, and the deletion gate
  whose reasoning the message tombstone reuses.
- **`stapel-realtime>=0.1.1` is a required dependency**, not an extra. The
  extra is now `stapel-chat[realtime]` and pulls Channels;
  `stapel-chat[channels]` stays as an alias for one release.
- `stapel_chat.views.SerializerSeamMixin` is re-exported from
  `stapel_core.django.api.views` — the local copy is deleted. A host that
  subclassed it by name keeps working.
- `routing.py` now mounts two patterns and documents
  `build_websocket_application()` as the host assembly. The hand-written
  `ProtocolTypeRouter` recipe is gone from the docs: three hand-written ASGI
  files are why the fleet had three auth stacks, and in the deployment that
  prompted this release, an origin guard that nobody had put in front of a
  cookie-authenticated socket.
- `unread_count` no longer counts tombstones — a message deleted before you
  read it must not leave a badge you can never clear by reading anything.
- `docs/llms.txt` budget raised deliberately to 4600 tokens (see the Makefile):
  the addressable surface roughly doubled in this release.

### Migration

```
pip install 'stapel-chat[realtime]>=0.3.0'
python manage.py migrate chat        # 0002 schema, 0003 backfill
python manage.py check               # E010-E014 will tell you what is missing
```

Host settings that are now required:

```python
INSTALLED_APPS = [..., "stapel_realtime", "stapel_chat"]
CHANNEL_LAYERS = {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer",
                              "CONFIG": {"hosts": [REDIS_URL]}}}
STAPEL_COMM = {..., "SIGNAL_TRANSPORT": "channels"}
STAPEL_REALTIME = {"ALLOWED_ORIGINS": ["https://app.example.com"]}  # WITH the port
```

and `asgi.py` becomes one call:

```python
from django.core.asgi import get_asgi_application
from stapel_realtime.asgi import build_websocket_application

application = build_websocket_application(http_application=get_asgi_application())
```

Migration `0003` backfills `rev_seq = seq` (a default of 0 would replay every
message on every resume) and converts `["<ref>"]` to `[{"key": "<ref>", "type":
"file"}]`. It cannot invent an aspect ratio or a thumbnail for a historical
row, so it refuses to guess: old attachments render as documents until a host
re-describes them from the CDN.

**Tests:** 155 pass (85 on 0.2.3) — 21 on the socket protocol, 16 on edit and
tombstone semantics, 23 on the attachment registry and the CDN contract, 19 on
the boot checks.

## [0.2.3] - 2026-08-24

### Fixed — history paging now requires the core `direction=prev` fix

`stapel-core` 0.36.0 fixed `AnchorPagination` returning the wrong page for
`direction=prev`. `test_prev_returns_newer_side` had been loosened to
`len(seqs) == 2 and all(s > 2 for s in seqs)` to tolerate the bug instead of
asserting the exact page; it now asserts `seqs == [4, 3]`, matching every
sibling test in the module. The floor on `stapel-core` moves to `>=0.36.0`
since `direction=prev` paging now relies on the fixed semantics.

## [0.2.2] - 2026-08-22

### Added — `routing.py` (the missing mount for the realtime consumer)

`ChatConsumer` has been resumable and store-first since 0.1.0 (hello →
welcome → seq replay → live, seq-dedup, `REPLAY_LIMIT`, `error{resync}`), but
every host had to invent its own mount path — none ever did, so the socket
shipped and stayed unmounted on every deployment, including the client fleet.

`stapel_chat.routing.websocket_urlpatterns` mounts it at
`ws/chat/<uuid:conversation_id>`, mirroring the one existing fleet precedent
(`stapel_video.routing`). Auth stays the host's job — wire it behind core's
G14 `stapel_core.django.jwt.channels.JWTAuthMiddlewareStack`, the same as
every other Channels consumer in the fleet; MODULE.md's new "Host ASGI
assembly" section has the copy-paste `asgi.py` block, since the scaffolder
does not generate this wiring yet.

No behavior change to the consumer itself and nothing breaks for a host that
already hand-rolled its own `routing.py` — this only adds the file that was
missing.

## [0.2.1] - 2026-08-21

### Fixed — `stapel_chat.E005` false positive on every NATS fleet

`E005` asks whether this deployment has workspaces, by asking whether
`workspaces.check_mandate` is reachable. Under `FUNCTION_TRANSPORT=nats` (or
any dotted custom transport), `comm.function_unreachable_reason` returns
`None` unconditionally — by its own docstring, nothing at boot can, or
should, prove a bus provider is up. The check read that "not provably
unreachable" as "workspaces present" and fired `E005` on every such fleet
running the shipped provider, whether or not workspaces was actually there
(found on a client NATS deploy).

`E005` now only fires where the answer is provable at boot — `inprocess`/
`http` `FUNCTION_TRANSPORT`, where the local registry or the route table
settles it. Over a bus transport it downgrades to a new `stapel_chat.W002`:
an honest "cannot verify" advisory rather than a guess dressed up as a
verdict. A deployment that genuinely has workspaces behind the bus and is
still running the shipped provider keeps its live tenancy hole either way —
`W002` says so in its own message — but it is not asserted as a boot-time
fact this process cannot actually check.

## [0.2.0] - 2026-08-16

### Security — ask who may operate before the participant row answers

`SupportAssignView` was first-come-served behind `IsAuthenticated`. Assigning
writes a `ConversationParticipant` with `role=OPERATOR`, and every later check
on the thread asked the participant table — which then answered with the row
the caller had just minted. One POST and a stranger's support conversation was
readable, postable and resolvable by an account holding no mandate anywhere.

- `ScopeProvider.can_operate(request, conversation=None)` — the question that
  comes *before* the participant row. The support queue, the claim, and
  resolve/reopen all ask it first. False means no; `MandateUnavailable` (503)
  means "could not find out" — admitting on a failed lookup is how the seam was
  open to begin with. Deliberately not applied to the customer half: a person
  opening a ticket typically holds no mandate, and refusing them would close
  the product to fix the door.
- `DefaultScopeProvider` answers it from the third principal state
  (`stapel_core.django.scope`), so a registered account with no mandate is not
  an operator of anything; in a genuinely standalone deployment it stays
  permissive and `checks.py` says so out loud.
- New system checks: `stapel_chat.E005` / `W001` — the shipped single-scope
  default carrying a multi-tenant host is now an error, not a silence.
  Importability and type were the only things ever validated here.

**Breaking for custom providers**: `can_operate` is abstract on `ScopeProvider`.
A host that subclasses it directly must implement the method (or mix in
`stapel_core.django.scope.MandateScopeMixin`), which is why this is 0.2.0 and
not a patch.

### Changed — `stapel-core` floor raised to 0.27.0

`django/scope.py` — `MandateScopeMixin` and `check_shipped_scope_provider` —
exists only in 0.27.0, and core owns `error.503.mandate_unavailable` in the
committed `docs/errors.json`.

## [0.1.9] - 2026-08-15

### Changed — `stapel-core` floor raised to 0.26.0

`docs/errors.json` carries an `owner` per entry, and only stapel-core 0.26.0
emits it. The floor lagged behind, so a consumer resolving an older core
regenerated an artifact without `owner` and the drift gate went red — the
field was declared but never required. The floor now matches the artifact
that is committed.

## [0.1.8] - 2026-08-02

### Added
- `docs/llms.txt` — the fifth contract artifact, an agent-sized slice of the
  schema/flows/errors/capabilities triad, wired into `make contract` /
  `make contract-check` (badge-canon §3).
- Badge canon in README, Python 3.14 classifier.
- CI matrix now tests Python 3.14 (the version actually in production),
  alongside the existing 3.11-3.13.

### Fixed
- `docs/capabilities.json`, `docs/flows.json`, `docs/errors.json`,
  `docs/llms.txt` and `CONFIG.MD` now ship in the wheel via `package-data`
  (#184); previously repo-only, invisible to `--from-installed` tooling.

## [0.1.6] - 2026-07-17

Fix-up #2: 0.1.5's regen still baked the old version into
`docs/capabilities.json` (`make contract` ran before the version bump
landed). Re-ran with 0.1.6 already in `pyproject.toml`; verified match,
suite green.

## [0.1.5] - 2026-07-17

Fix-up: 0.1.4's CI/publish failed on contract drift — `docs/capabilities.json`
embeds the package version and wasn't regenerated for the 0.1.4 bump.
Regenerated via `make contract`; no other diff.

## [0.1.4] - 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep). No source
changes needed. Full suite green against core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13` (base + `channels`/`all` extras).

## [0.1.3] - 2026-07-17

### Removed
- Dead `default_app_config` in the package `__init__` — the pattern was
  deprecated in Django 3.2 and removed in Django 4.1; `AppConfig` is
  auto-discovered. Inert on every supported Django, no behavior change.

## [0.1.2] - 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` in both the
  base dependency and the `channels` extra (core 0.11 fleet re-pin:
  default bus, nav, config-checks, error params/language — additive for
  modules).
- Contract artifacts regenerated (version bump); no other drift.

## [0.1.0] - 2026-07-10

Initial alpha release.

### Added
- **Conversations** — one model, three kinds: `direct` (1:1, idempotent by
  participant pair per scope), `group`, and `support`. Participants carry a
  role (`member` / `operator`) and a per-participant read marker.
- **Messages** — monotonic per-conversation `seq` (gapless, allocated under a
  row lock with a unique-constraint + retry backstop), `text` / `system` kinds,
  optional reply, and opaque attachment keys (files live in the host's CDN —
  the module stores keys only).
- **Send path** — persist the row and emit `chat.message` in one transaction
  (outbox / `mutate_and_emit`); best-effort realtime fan-out scheduled
  `on_commit`.
- **Read markers & unread counts**, **mark-read** (monotonic).
- **History & conversation lists** — anchor-paginated (core `AnchorPagination`);
  message history anchors on `seq`, newest-first, both directions.
- **Support layer** — unassigned queue, first-come `assign` (emits
  `chat.support.assigned`), `open` / `pending` / `resolved` statuses with
  `reopen`.
- **Realtime** — optional Channels consumer (`stapel_chat.consumers.ChatConsumer`)
  on `stapel_core.django.jwt.channels`: live delivery, resume-by-`seq` replay,
  resync on too-wide gaps. Correctness never depends on delivery.
- **Config axes** — `CHAT_KINDS`, `ATTACHMENTS`, `MAX_BODY_LENGTH`; the one
  extension seam is `SCOPE_PROVIDER`.
- **Contract** — per-module `docs/{schema,flows,errors,capabilities}.json` +
  emit/consume JSON schemas; GDPR `user.deleted` handler.

[0.1.0]: https://github.com/usestapel/stapel-chat/releases/tag/v0.1.0
