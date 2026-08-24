# CONFIG.MD — stapel-chat

Config registry for **stapel-chat** (`static-scaffold-and-config.md` §2). One
row per configuration key the module reads, its **source** (`env` = the process
environment / the `STAPEL_CHAT` settings dict), what it is for, whether it is
required, and its default.

Every key is read through `chat_settings` (`stapel_chat.conf.AppSettings`,
namespace `STAPEL_CHAT`). Resolution order per key: `settings.STAPEL_CHAT` dict →
a flat Django setting of the same name → environment variable → the default
below.

**There is no key that turns realtime off.** Since 0.3.0 the WebSocket is the
path a message takes, and a deployment that cannot serve one fails
`manage.py check` (`stapel_chat.E010`–`E014`) instead of degrading into a
product that refreshes on a timer. A knob would reproduce the defect: the
deployment that shipped "updates every few seconds" never chose it either.

## stapel-chat

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| CHAT_KINDS | env | axis: enabled subset of {direct, group, support}. Dropping `support` removes the operator queue; dropping `group` leaves only 1:1. | no | [direct, group, support] |
| ATTACHMENTS | env | axis: whether messages may carry attachments (opaque CDN key + render metadata; no files stored). When off, an attachment-bearing message is rejected. | no | true |
| MAX_BODY_LENGTH | env | axis: hard cap on a text message body (characters). | no | 4000 |
| ATTACHMENT_TYPES | settings | OPEN registry, merged over the builtins {image, gif, video, voice, file}; `None` removes one. Where a host adds stickers. | no | {} |
| ACTIVITY_STATES | settings | OPEN registry, merged over the builtins {idle, typing, recording_audio, sending_video, uploading_file}; `None` removes one. | no | {} |
| ATTACHMENT_METADATA | env | axis: `cdn` asks `cdn.describe` by comm and merges its answer over the client's; `client` trusts the sender and makes no call. | no | cdn |
| MAX_ATTACHMENTS | env | axis: attachments per message. | no | 10 |
| MAX_PREVIEW_B64_BYTES | env | axis: ceiling on an inline `data:` preview (micro-thumbnail / waveform) — untrusted bytes rendered on other people's screens. | no | 8192 |
| EDIT_WINDOW_S | env | axis: seconds an author may still edit their own message; 0 = no deadline. | no | 0 |
| SCOPE_PROVIDER | env | dotted path to a ScopeProvider (resolve/filter the opaque scope_key). | no | stapel_chat.scope.DefaultScopeProvider |

## Host settings this module requires (not its own namespace)

These live in the project's settings, and `manage.py check` refuses to boot
without them:

| Setting | Why | Check |
|---|---|---|
| `INSTALLED_APPS` contains `stapel_realtime` | both consumers, the wire envelope and the signal transport are the substrate's | `stapel_chat.E010` |
| `CHANNEL_LAYERS["default"]` | without it the fan-out is a no-op: sockets connect, replay once, then go silent | `stapel_chat.E011` |
| A WebSocket credential the browser can actually send | a browser cannot set `Authorization` on `new WebSocket()` | `stapel_chat.E012` |
| `STAPEL_COMM["SIGNAL_TRANSPORT"] = "channels"` | typing, receipts and the live inbox are Signals, silently dropped otherwise | `stapel_chat.E013` |
| `STAPEL_REALTIME["ALLOWED_ORIGINS"]` (with ports) | a cookie is ambient authority; an unguarded socket is CSWSH of a live conversation | `stapel_chat.E014` |
