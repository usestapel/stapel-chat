# Changelog

All notable changes to stapel-chat are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
