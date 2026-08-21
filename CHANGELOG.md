# Changelog

All notable changes to stapel-chat are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.2] - 2026-08-22

### Added — `routing.py` (the missing mount for the realtime consumer)

`ChatConsumer` has been resumable and store-first since 0.1.0 (hello →
welcome → seq replay → live, seq-dedup, `REPLAY_LIMIT`, `error{resync}`), but
every host had to invent its own mount path — none ever did, so the socket
shipped and stayed unmounted on every deployment, including the darom fleet.

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
(found on the darom.ai NATS deploy).

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
