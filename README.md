# stapel-chat

[![CI](https://img.shields.io/github/actions/workflow/status/usestapel/stapel-chat/ci.yml?branch=main&logo=github&label=CI)](https://github.com/usestapel/stapel-chat/actions/workflows/ci.yml?query=branch%3Amain)
[![coverage](https://img.shields.io/codecov/c/github/usestapel/stapel-chat?branch=main&logo=codecov&label=coverage)](https://app.codecov.io/gh/usestapel/stapel-chat)
[![pypi](https://img.shields.io/pypi/v/stapel-chat?logo=pypi&logoColor=white&label=pypi)](https://pypi.org/project/stapel-chat/)
[![downloads](https://static.pepy.tech/badge/stapel-chat/month)](https://pepy.tech/project/stapel-chat)
[![python](https://img.shields.io/pypi/pyversions/stapel-chat?logo=python&logoColor=white)](https://pypi.org/project/stapel-chat/)
[![license](https://img.shields.io/github/license/usestapel/stapel-chat)](https://github.com/usestapel/stapel-chat/blob/main/LICENSE)
[![llms.txt](https://img.shields.io/badge/llms.txt-blue)](https://github.com/usestapel/stapel-chat/blob/main/docs/llms.txt)

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
