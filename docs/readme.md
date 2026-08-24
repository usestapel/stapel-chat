One model backs three kinds of thread: **direct** (1:1, idempotent by
participant pair), **group**, and **support** (a customer↔operator thread with a
queue and assignment lifecycle).

**Realtime is the module, not a mode of it.** Messages are sent and received
over a WebSocket; REST serves history, hydration and the support lifecycle.
A deployment that cannot serve the socket fails `manage.py check` rather than
degrading into a product that refreshes on a timer — because a polling fallback
a product can end up in silently is exactly how "websockets are done" became a
false claim once already.

## Quick start

```python
INSTALLED_APPS = [
    # ...
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
    "rest_framework",
    "stapel_realtime",
    "stapel_chat",
]

CHANNEL_LAYERS = {"default": {
    "BACKEND": "channels_redis.core.RedisChannelLayer",
    "CONFIG": {"hosts": [REDIS_URL]},
}}
STAPEL_COMM = {"SIGNAL_TRANSPORT": "channels"}
STAPEL_REALTIME = {"ALLOWED_ORIGINS": ["https://app.example.com"]}  # with the port

# urls.py
urlpatterns = [path("chat/", include("stapel_chat.urls"))]
```

```python
# asgi.py — the whole file
from django.core.asgi import get_asgi_application
from stapel_realtime.asgi import build_websocket_application

application = build_websocket_application(http_application=get_asgi_application())
```

```
pip install 'stapel-chat[realtime]'
```

## What you get

- **Two sockets.** `ws/chat/<conversation_id>` is the resumable journal —
  `hello{last_seq}` → replay → live, with `send` / `edit` / `delete` / `read` /
  `delivered` / `activity` frames going through the same service layer the REST
  views call. `ws/chat/inbox` keeps the conversation list live, because a list
  with no socket refreshes on a timer forever however live the open thread is.
- **Two sequences.** `seq` is a message's immutable place in the thread — the
  sort key and the history anchor. `rev_seq` is its place in the revision
  journal, re-allocated on every edit and delete, and it is what realtime
  replay is anchored on: an edit made while a client was offline arrives in the
  catch-up. A client upserts by id, sorts by `seq`, and remembers `rev_seq` as
  its cursor.
- **Edit and delete.** An edit sets `edited` / `edited_at`. A delete leaves a
  **tombstone**: the id keeps being delivered with `body: ""`,
  `attachments: []` and `deleted: true`, so a client cache learns which id to
  purge. An id that stops arriving is an id nobody can purge. Retention is
  permanent.
- **Attachments that render on first paint** — aspect, byte size and a ~16px
  base64 thumbnail for images and GIFs; duration and a waveform image for voice;
  mime and extension for documents; poster and duration for video. The type set
  is an **open registry** — stickers are a settings line — and the metadata comes
  from `stapel-cdn` by comm, once, at send time.
- **Receipts and activity.** Separate delivery and read markers, both durable
  and both fanned out live; `typing` / `recording_audio` / `sending_video` /
  `uploading_file` as ephemeral signals with a TTL, from another open registry.
- **Conversations** — `POST /chat/api/v1/conversations` (`direct` / `group` /
  `support`); direct is get-or-create by participant pair. `GET` lists yours
  (anchor-paginated) with `unread_count`, and every row carries its own
  `stream_key` and `socket_path`.
- **Messages** — `GET/POST /chat/api/v1/conversations/{id}/messages`,
  `PATCH/DELETE .../messages/{message_id}`. History is anchored on `seq`,
  newest-first, both directions.
- **Support** — `GET /chat/api/v1/support/queue`,
  `POST .../support/conversations/{id}/{assign,resolve,reopen}`.

## Configuration (`STAPEL_CHAT`)

| Key | Default | Meaning |
|---|---|---|
| `CHAT_KINDS` | `["direct","group","support"]` | Enabled thread kinds |
| `ATTACHMENTS` | `True` | Allow attachments on messages |
| `MAX_BODY_LENGTH` | `4000` | Hard cap on a text body |
| `ATTACHMENT_TYPES` | `{}` | Open registry, merged over `image/gif/video/voice/file` |
| `ACTIVITY_STATES` | `{}` | Open registry, merged over `typing/recording_audio/…` |
| `ATTACHMENT_METADATA` | `"cdn"` | Ask `cdn.describe`, or trust the client |
| `MAX_ATTACHMENTS` | `10` | Attachments per message |
| `MAX_PREVIEW_B64_BYTES` | `8192` | Ceiling on an inline `data:` preview |
| `EDIT_WINDOW_S` | `0` | Seconds a message stays editable (0 = forever) |
| `SCOPE_PROVIDER` | `stapel_chat.scope.DefaultScopeProvider` | Resolve/enforce the opaque `scope_key` |

There is no key that turns realtime off. See
[MODULE.md](https://github.com/usestapel/stapel-chat/blob/main/MODULE.md) for
the full wire contract, the extension seams and the anti-patterns.
