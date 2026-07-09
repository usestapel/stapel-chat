# stapel-chat

Conversations, messaging and support chat for the [Stapel](https://github.com/usestapel)
framework — a reusable Django app you mount in a host project.

One model backs three kinds of thread: **direct** (1:1, idempotent by
participant pair), **group**, and **support** (a customer↔operator thread with a
queue and assignment lifecycle). Messages carry a monotonic per-conversation
**seq** that is the canonical anchor for history pagination and the resume
cursor for realtime — so nothing depends on a socket staying up.

```python
INSTALLED_APPS = [
    # ...
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
    "rest_framework",
    "stapel_chat",
]

# urls.py
urlpatterns = [
    path("chat/", include("stapel_chat.urls")),
]
```

## What you get

- **Conversations** — `POST /chat/api/conversations` (`direct` / `group` /
  `support`); direct is get-or-create by participant pair. `GET` lists your
  conversations (anchor-paginated) with `unread_count`.
- **Messages** — `GET/POST /chat/api/conversations/{id}/messages`. History is
  anchored on `seq` (newest-first, both directions). Sending allocates the next
  `seq` and emits `chat.message` in one transaction.
- **Read markers** — `POST /chat/api/conversations/{id}/read` (`upto_seq`).
- **Support** — `GET /chat/api/support/queue`,
  `POST .../support/conversations/{id}/{assign,resolve,reopen}`.
- **Realtime (optional)** — `stapel_chat.consumers.ChatConsumer` over Channels:
  `hello{last_seq}` → replay by seq → live delivery. Install the extra:

  ```
  pip install 'stapel-chat[channels]'
  ```

  and wire it behind `stapel_core.django.jwt.channels.JWTAuthMiddlewareStack` in
  your `asgi.py`.

## Configuration (`STAPEL_CHAT`)

| Key | Default | Meaning |
|---|---|---|
| `CHAT_KINDS` | `["direct","group","support"]` | Enabled thread kinds (drop `support` to disable the operator queue) |
| `ATTACHMENTS` | `True` | Allow opaque attachment keys on messages |
| `MAX_BODY_LENGTH` | `4000` | Hard cap on a text body |
| `SCOPE_PROVIDER` | `stapel_chat.scope.DefaultScopeProvider` | Resolve/enforce the opaque `scope_key` (e.g. per workspace) |

See [MODULE.md](MODULE.md) for the extension seams, comm surface and
anti-patterns.

## License

MIT — see [LICENSE](LICENSE).
