# CONFIG.MD — stapel-chat

Config registry for **stapel-chat** (`static-scaffold-and-config.md` §2). One
row per configuration key the module reads, its **source** (`env` = the process
environment / the `STAPEL_CHAT` settings dict), what it is for, whether it is
required, and its default.

Every key is read through `chat_settings` (`stapel_chat.conf.AppSettings`,
namespace `STAPEL_CHAT`). Resolution order per key: `settings.STAPEL_CHAT` dict →
a flat Django setting of the same name → environment variable → the default
below. `CHAT_KINDS`, `ATTACHMENTS` and `MAX_BODY_LENGTH` are CTO-facing config
axes (capability-config.md §16).

## stapel-chat

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| CHAT_KINDS | env | axis: enabled subset of {direct, group, support}. Dropping `support` removes the operator queue; dropping `group` leaves only 1:1. | no | [direct, group, support] |
| ATTACHMENTS | env | axis: whether messages may carry attachment keys (opaque CDN keys; no files stored). When off, an attachment-bearing message is rejected. | no | true |
| MAX_BODY_LENGTH | env | axis: hard cap on a text message body (characters). | no | 4000 |
| SCOPE_PROVIDER | env | dotted path to a ScopeProvider (resolve/filter the opaque scope_key). | no | stapel_chat.scope.DefaultScopeProvider |
