"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* an operation that genuinely cannot run in-process is listed by name in
  ``UNDRIVABLE`` with a one-line reason, asserted exactly current;
* a collection that comes back empty fails in the populated pass — an empty
  array validates against any item schema, so an empty answer is a check that
  looked at nothing;
* every read, and every write whose declared body carries a nullable field,
  is driven a SECOND time in its emptiest legal state (``EMPTY_STATE``): a
  thread with a subject, a last line, an operator and a page of history, and
  a thread that has none of them. Every null finding in the first wave of
  this gate was on the empty state.

Runs on every interpreter: it reads the committed schema and never emits.

THE MOUNT. ``codegen_urls.py`` mounts ``chat/`` and this module's own
``urls.py`` bakes ``api/v1/`` in, so the document is written against
``/chat/api/v1/…``; ``stapel_chat/tests/urls.py`` mounts the SAME prefix, so
unlike five of the first eight libraries in this wave this module's suite was
already looking where its document points. The emission mount is declared
here anyway, so a later edit to the test urlconf cannot silently unhook the
contract.

WHAT IT FOUND on its first run — 11 of 11 operations driven, 10 of them a
second time in their emptiest state, 1 red:

* ``GET /conversations/{id}/messages`` declares
  ``PaginatedMessageResponseList.next_anchor`` / ``prev_anchor`` as
  ``string`` (nullable) and sends an **integer** on every page that has a
  neighbour. ``MessageHistoryPagination.anchor_field`` is ``seq``
  (views.py:101-111), an ``IntegerField``, and
  ``AnchorPagination.get_paginated_response`` copies the raw field value into
  the envelope — it stringifies only values carrying ``.isoformat()``
  (stapel-core ``django/api/pagination.py``:239-266), which an int does not.
  The module's THREE other paginators all anchor on a datetime
  (``updated_at``, ``created_at``, ``viewer_left_at``), where that branch
  fires and the same declaration is true — which is exactly why the one int
  anchor went unnoticed. A generated client types ``next_anchor`` as
  ``string | null`` and hands ``7`` back as the ``anchor`` query parameter,
  which happens to work: nothing fails loudly, TypeScript simply believes a
  lie about every conversation longer than one page. This is the SAME defect
  stapel-recordings carries on its own ``seq`` anchor, found in the same
  sweep — one shared paginator, two callers that anchor on an int.

Everything else held, including every ``nullable`` field of
``ConversationResponse``, ``MessageResponse``, ``ParticipantResponse``,
``AttachmentResponse`` and ``LastMessageResponse`` in both states, and
``test_the_gate_is_not_blind`` proves that is a finding rather than a gate
that never looked.

Left exactly as it is: this is a gate, not a fix.
"""
import copy
import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client
#: (``codegen_urls.py``: ``chat/`` + the module's own ``api/v1/``).
urlpatterns = [
    url_path("chat/", include("stapel_chat.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/chat/api/v1"

#: The subject type this gate registers, and the comm Function that renders
#: its card. Both are SEAMS a deployment wires: the registry ships empty
#: (``SUBJECT_TYPES`` default ``{}``) on purpose, because a subject nobody can
#: render is a string in a database.
SUBJECT_TYPE = "listing"
CARD_FUNCTION = "wire.subject_cards"


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Pin ``MEDIA_ROOT`` for the duration of a test.

    Nothing in this module writes a file — an attachment is an opaque CDN ref
    and the bytes never travel through here (models.py: no file storage) —
    but ``MEDIA_ROOT`` is unset in the harness settings, so it defaults to the
    working directory and one future upload would land in the checkout. That
    is how an export in stapel-auth ended up beside a flat-layout package,
    where a stray directory then shadowed a real module.
    """
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


@pytest.fixture(autouse=True)
def _seams():
    """The two sibling-owned seams, wired the way a deployment wires them.

    ``<subject type>.card_function`` is stapel-classified's (or whatever owns
    the subject); ``cdn.describe_many`` is stapel-cdn's. This module never
    imports either — it calls them by name over the comm registry, and that
    name is the seam a deployment fills. Standing them up here is what makes
    the NON-null half of ``subject`` and of every ``AttachmentResponse``
    field reachable; everything on this side of the seam (the DTO, the
    serializer, the status) runs for real.

    The runtime subject registry is reset on both sides: a function name has
    exactly one provider process-wide, so leaving either behind would hand
    the next test this one's stand-in.
    """
    from stapel_core.comm.registry import function_registry

    from stapel_chat.subjects import register_subject_type, reset_subject_types

    reset_subject_types()
    # Displaced, not saved: the registry ships this function empty on purpose
    # and every test that wants it registers its own provider.
    function_registry._providers.pop(CARD_FUNCTION, None)
    function_registry._schemas.pop(CARD_FUNCTION, None)

    def cards(payload):
        return {
            "cards": {
                key: {"title": "A thing for sale", "price": "10.00"}
                for key in payload.get("keys") or []
            }
        }

    function_registry.register(CARD_FUNCTION, cards)
    register_subject_type(SUBJECT_TYPE, {"card_function": CARD_FUNCTION})
    try:
        yield
    finally:
        reset_subject_types()
        function_registry._providers.pop(CARD_FUNCTION, None)
        function_registry._schemas.pop(CARD_FUNCTION, None)


class describe_seam:
    """Stand in for stapel-cdn's ``cdn.describe_many``, for one call."""

    NAME = "cdn.describe_many"

    def __init__(self, snapshot=None):
        self.snapshot = snapshot

    def __enter__(self):
        from stapel_core.comm.registry import function_registry

        self._previous = function_registry._providers.pop(self.NAME, None)
        function_registry._schemas.pop(self.NAME, None)
        snapshot = self.snapshot

        def describe(payload):
            refs = list(payload.get("refs") or [])
            if snapshot is None:
                return {"items": {}, "missing": refs}
            return {"items": {ref: dict(snapshot, ref=ref) for ref in refs}, "missing": []}

        function_registry.register(self.NAME, describe)
        return self

    def __exit__(self, *exc):
        from stapel_core.comm.registry import function_registry

        function_registry._providers.pop(self.NAME, None)
        function_registry._schemas.pop(self.NAME, None)
        if self._previous is not None:
            function_registry._providers[self.NAME] = self._previous
        return False


#: A full stapel-cdn snapshot — the state in which every nullable field of
#: ``AttachmentResponse`` carries a value rather than a null.
FULL_SNAPSHOT = {
    "kind": "image",
    "mime": "image/jpeg",
    "ext": ".jpg",
    "bytes": 51234,
    "width": 1600,
    "height": 900,
    "aspect": 1.777778,
    "square": False,
    "animated": False,
    "duration_ms": None,
    "preview_b64": "data:image/webp;base64,UklGRh",
    "preview_kind": "blur",
    "poster_url": None,
    "meta_status": "ok",
    "meta_reason": None,
    "variants": [{"tier": 720, "branch": "w", "url": "/x/720.webp"}],
}


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _undiscriminated_union(node):
    """A ``oneOf`` whose branches OVERLAP by construction.

    Only the REQUEST side of this contract has one (an attachment is either an
    object or a bare pre-0.3 ref string), so nothing this gate validates goes
    through it today. The conversion stays because a response union would be
    read the same way: without a ``discriminator`` the branches are
    alternatives, and an exclusive ``oneOf`` would reject what the document
    plainly describes.
    """
    branches = node.get("oneOf")
    if not isinstance(branches, list) or "discriminator" in node:
        return False
    return len(branches) > 1


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type`` (or
    beside an ``allOf`` wrapping a ``$ref``, which is how ``subject`` and
    ``last_message`` are emitted); JSON Schema has no such keyword and would
    refuse the null — which is exactly the value most of these fields answer
    in their empty state. Everything else drf-spectacular emits here
    (``$ref``, ``allOf``, ``format``, ``required``, ``additionalProperties``)
    is JSON Schema as written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if _undiscriminated_union(rebuilt):
        rebuilt["anyOf"] = rebuilt.pop("oneOf")
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def make_user():
    return get_user_model().objects.create_user(
        username=_unique("wire-"),
        email=f"{_unique('wire-')}@example.com",
        password="wire-contract-password-7",
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def direct(owner=None, other=None, subject=True):
    """A direct thread, with or without the subject it is about."""
    from stapel_chat import services

    owner = owner or make_user()
    other = other or make_user()
    extra = (
        {"subject_type": SUBJECT_TYPE, "subject_key": _unique("listing-")}
        if subject
        else {}
    )
    conv = services.create_direct(owner=owner, other_user_id=other.id, **extra)
    return owner, other, conv


def support(customer=None):
    from stapel_chat import services

    customer = customer or make_user()
    return customer, services.create_support(customer=customer)


def assigned_support(operator=None):
    """A support thread with an operator already on it."""
    from stapel_chat import services

    operator = operator or make_user()
    customer, conv = support()
    return operator, customer, services.assign_operator(conversation=conv, operator=operator)


def say(conv, sender, body="hello", **kwargs):
    from stapel_chat import services

    return services.post_message(
        conversation=conv, sender=sender, body=body, **kwargs
    )


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method == "GET":
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template, status code)``.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe. Every null finding in the first wave of this gate was there.
EMPTY_STATE = {}


def recipe(method, path, code=None, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path, code)
        assert key not in target, f"duplicate recipe for {method} {path} {code}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path, code=None):
    return recipe(method, path, code, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY. Every operation this module declares is reachable from a test
#: client. The three sibling-owned calls it makes (the subject card function,
#: ``cdn.describe_many``, and the block check) are comm SEAMS a deployment
#: wires, so the gate wires them too rather than skipping the paths that use
#: them.
UNDRIVABLE: dict = {}


# ── the inbox ────────────────────────────────────────────────────────────────


@recipe("GET", "/conversations")
def _inbox(call):
    """A thread with everything an inbox row draws: a subject card, a last
    line, an unread count and two participants."""
    owner, other, conv = direct()
    say(conv, other, "the line the row draws")
    return call(client_for(owner))


@empty_state("GET", "/conversations")
def _inbox_empty(call):
    """An account that has talked to nobody — ``items`` is genuinely []."""
    return call(client_for(make_user()))


@recipe("POST", "/conversations", code=201)
def _open_thread(call):
    owner = make_user()
    other = make_user()
    return call(
        client_for(owner),
        data={
            "kind": "direct",
            "participant_ids": [str(other.id)],
            "subject_type": SUBJECT_TYPE,
            "subject_key": _unique("listing-"),
        },
    )


@empty_state("POST", "/conversations", code=201)
def _open_thread_bare(call):
    """A support thread, which is about the deployment and not about an
    object in it: null ``subject``, null ``last_message``, null
    ``assigned_operator_id``, null ``left_at``, null ``cleared_at`` — five
    nullable claims answered at once, and the state every thread starts in."""
    return call(client_for(make_user()), data={"kind": "support"})


@recipe("GET", "/conversations/{conversation_id}")
def _thread(call):
    owner, other, conv = direct()
    say(conv, other, "the line the header draws")
    return call(client_for(owner), params={"conversation_id": conv.id})


@empty_state("GET", "/conversations/{conversation_id}")
def _thread_empty(call):
    """Opened and never used: no subject, no message, nobody assigned."""
    owner, _other, conv = direct(subject=False)
    return call(client_for(owner), params={"conversation_id": conv.id})


# ── history ──────────────────────────────────────────────────────────────────


@recipe("GET", "/conversations/{conversation_id}/messages")
def _history(call):
    """One message per page, so the envelope actually carries an anchor.

    The default page size is far above three — a history that fits in one
    page answers ``next_anchor: null`` and says nothing about the type the
    field carries when there IS a next page, which is the claim this
    operation gets wrong.
    """
    owner, other, conv = direct()
    for index in range(3):
        say(conv, other, f"line {index}")
    return call(
        client_for(owner), params={"conversation_id": conv.id}, query="?limit=1"
    )


@empty_state("GET", "/conversations/{conversation_id}/messages")
def _history_empty(call):
    """A thread nobody has written in: an empty page, and both anchors
    genuinely null — the state the declaration describes correctly."""
    owner, _other, conv = direct(subject=False)
    return call(client_for(owner), params={"conversation_id": conv.id})


@recipe("POST", "/conversations/{conversation_id}/messages", code=201)
def _send(call):
    """Everything a message can carry: a reply, an attachment the CDN knows
    all about, and the sender's own idempotency key."""
    owner, other, conv = direct()
    earlier = say(conv, other, "the line being replied to")
    with describe_seam(FULL_SNAPSHOT):
        return call(
            client_for(owner),
            params={"conversation_id": conv.id},
            data={
                "body": "a reply",
                "reply_to": str(earlier.id),
                "client_msg_id": _unique("client-"),
                "attachments": [{"key": f"avatar/{'a1' * 32}", "type": "image"}],
            },
        )


@empty_state("POST", "/conversations/{conversation_id}/messages", code=201)
def _send_bare(call):
    """Body and nothing else: null ``reply_to``, no attachments, and the
    null ``edited_at`` / ``deleted_at`` every fresh message carries."""
    owner, _other, conv = direct(subject=False)
    return call(
        client_for(owner), params={"conversation_id": conv.id}, data={"body": "hello"}
    )


@recipe("PATCH", "/conversations/{conversation_id}/messages/{message_id}")
def _edit(call):
    owner, _other, conv = direct()
    mine = say(conv, owner, "a typo")
    return call(
        client_for(owner),
        params={"conversation_id": conv.id, "message_id": mine.id},
        data={"body": "corrected"},
    )


@empty_state("PATCH", "/conversations/{conversation_id}/messages/{message_id}")
def _edit_unresolvable_attachment(call):
    """Editing a message whose attachment the CDN cannot resolve: every
    ``AttachmentResponse`` field null, ``meta_status`` saying which — the
    "one dead attachment does not cost the other nine" branch."""
    owner, _other, conv = direct(subject=False)
    with describe_seam(None):
        mine = say(
            conv,
            owner,
            "a typo",
            attachments=[{"key": f"avatar/{'d4' * 32}", "type": "image"}],
        )
        return call(
            client_for(owner),
            params={"conversation_id": conv.id, "message_id": mine.id},
            data={"body": "corrected"},
        )


@recipe("DELETE", "/conversations/{conversation_id}/messages/{message_id}")
def _delete(call):
    """A tombstone, answered 200 with the stripped row on purpose: the body
    is what a local cache purges against."""
    owner, _other, conv = direct()
    mine = say(conv, owner, "sent by mistake")
    return call(
        client_for(owner),
        params={"conversation_id": conv.id, "message_id": mine.id},
    )


@empty_state("DELETE", "/conversations/{conversation_id}/messages/{message_id}")
def _delete_attachment_message(call):
    """Deleting a message that carried an attachment: the tombstone must drop
    it, so this is the emptiest ``attachments`` a row can answer with."""
    owner, _other, conv = direct(subject=False)
    with describe_seam(FULL_SNAPSHOT):
        mine = say(
            conv,
            owner,
            "sent by mistake",
            attachments=[{"key": f"avatar/{'a1' * 32}", "type": "image"}],
        )
    return call(
        client_for(owner),
        params={"conversation_id": conv.id, "message_id": mine.id},
    )


# ── the support surface ──────────────────────────────────────────────────────


@recipe("GET", "/support/queue")
def _queue(call):
    customer, conv = support()
    say(conv, customer, "my order never arrived")
    return call(client_for(make_user()))


@empty_state("GET", "/support/queue")
def _queue_empty(call):
    """Nothing waiting — the state an operator opens the day on."""
    return call(client_for(make_user()))


@recipe("POST", "/support/conversations/{conversation_id}/assign")
def _assign(call):
    customer, conv = support()
    say(conv, customer, "my order never arrived")
    return call(client_for(make_user()), params={"conversation_id": conv.id})


@empty_state("POST", "/support/conversations/{conversation_id}/assign")
def _assign_untouched(call):
    """Claiming a thread nobody has written in: null ``last_message``, null
    ``subject``, null ``left_at``, null ``cleared_at``."""
    _customer, conv = support()
    return call(client_for(make_user()), params={"conversation_id": conv.id})


@recipe("POST", "/support/conversations/{conversation_id}/resolve")
def _resolve(call):
    operator, customer, conv = assigned_support()
    say(conv, customer, "my order never arrived")
    return call(client_for(operator), params={"conversation_id": conv.id})


@empty_state("POST", "/support/conversations/{conversation_id}/resolve")
def _resolve_untouched(call):
    operator, _customer, conv = assigned_support()
    return call(client_for(operator), params={"conversation_id": conv.id})


@recipe("POST", "/support/conversations/{conversation_id}/reopen")
def _reopen(call):
    from stapel_chat import services

    operator, customer, conv = assigned_support()
    say(conv, customer, "my order never arrived")
    services.resolve_support(conversation=conv)
    return call(client_for(operator), params={"conversation_id": conv.id})


@empty_state("POST", "/support/conversations/{conversation_id}/reopen")
def _reopen_untouched(call):
    from stapel_chat import services

    operator, _customer, conv = assigned_support()
    services.resolve_support(conversation=conv)
    return call(client_for(operator), params={"conversation_id": conv.id})


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


_SEQ_ANCHOR = (
    "PaginatedMessageResponseList.next_anchor/prev_anchor are declared "
    "`string` (nullable) and the wire sends an INTEGER on every page that has "
    "a neighbour. MessageHistoryPagination.anchor_field is `seq` "
    "(views.py:101-111), an IntegerField on Message, and "
    "AnchorPagination.get_paginated_response copies the raw field value into "
    "the envelope, stringifying only values that carry `.isoformat()` — which "
    "an int never does. The module's three OTHER paginators all anchor on a "
    "datetime (updated_at, created_at, viewer_left_at), where that branch "
    "fires and the same declaration is true, which is why the one int anchor "
    "went unnoticed. OWNER: this module's MessageHistoryPagination — the "
    "envelope's schema comes from the shared "
    "AnchorPagination.get_paginated_response_schema (stapel-core "
    "django/api/pagination.py:300-331), which hardcodes `type: string` and is "
    "honest for every datetime-anchored caller; the int anchor is this "
    "module's choice. stapel-recordings carries the identical defect on its "
    "own `seq` anchor — one shared paginator, two callers that anchor on an "
    "int — so a fix in core's schema (deriving the anchor type from the "
    "field) would close both. The EMPTY state of this operation is honest and "
    "is driven separately."
)

#: Operations whose declared body the POPULATED wire does not send.
#:
#: An entry names the defect AND its owner, and ``strict=True`` turns a fixed
#: one into a failure until the entry is deleted, so a finding can be neither
#: forgotten nor quietly kept.
KNOWN_MISMATCHES = {
    ("GET", V1 + "/conversations/{conversation_id}/messages"): _SEQ_ANCHOR,
}

#: The same, for the EMPTY-state pass. Separate on purpose: a defect can live
#: in one state and not the other, and marking both xfail would hide a claim
#: the wire actually keeps. EMPTY here — an empty history page answers both
#: anchors null, which is the one shape this declaration gets right.
KNOWN_MISMATCHES_EMPTY: dict = {}


def _recipe_for(table, method, path, code):
    """The code-specific recipe if there is one, else the operation's."""
    return table.get((method, path, code)) or table.get((method, path, None))


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Five of the first eight libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, one mounted less
    than the emission did, one doubled a segment to reproduce a host's
    deployed prefix.

    A missing recipe already fails loudly; this fails when the MOUNT is wrong,
    which no per-operation check can see, because when the mount is wrong
    every operation is equally and silently unreachable.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment. A path counts as
    # reachable if any one shape resolves: the question here is whether the
    # mount exists, not whether a particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    missing = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if _recipe_for(RECIPES, method, path, code) is None
        and (method, path) not in UNDRIVABLE
    ]
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in missing)
    )

    declared_codes = {(m, p, c) for m, p, c, _ in OPERATIONS}
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted(
        key
        for key in RECIPES
        if (key[0], key[1]) not in declared_ops
        or (key[2] is not None and key not in declared_codes)
    )
    assert not stale, (
        "recipes for operations/status codes the contract no longer declares:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in stale)
    )
    stale_exclusions = sorted(set(UNDRIVABLE) - declared_ops)
    assert not stale_exclusions, (
        f"exclusions for operations the contract no longer declares: {stale_exclusions}"
    )
    both = sorted((m, p) for m, p, _c in RECIPES if (m, p) in UNDRIVABLE)
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"

    # RECIPES ∪ UNDRIVABLE is EXACTLY the declared set, in both directions.
    covered = {(m, p) for m, p, _c in RECIPES} | set(UNDRIVABLE)
    assert covered == declared_ops, (
        "the covered set and the declared set differ:\n"
        f"  declared and not covered: {sorted(declared_ops - covered)}\n"
        f"  covered and not declared: {sorted(covered - declared_ops)}"
    )


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state.
    A gate that only ever seeds three rows and asks never sees any of them.

    So every GET is required to have an ``EMPTY_STATE`` recipe as well, and so
    is every write whose declared body carries a nullable field — which here
    is all of them, because ``ConversationResponse`` and ``MessageResponse``
    both do. The exemptions are named here, each with its reason.
    """
    exempt: set = set()
    reads = {(m, p) for m, p, _c, _ in OPERATIONS}
    covered = {(m, p) for m, p, _c in EMPTY_STATE}
    missing = sorted(reads - covered - exempt)
    assert not missing, (
        "operations driven only against a populated database — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted({(m, p) for m, p, _c in EMPTY_STATE} - declared_ops)
    assert not stale, f"empty-state recipes for undeclared operations: {stale}"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason."""
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for table in (KNOWN_MISMATCHES, KNOWN_MISMATCHES_EMPTY):
        for key, reason in table.items():
            assert key in declared, (
                f"{key} is recorded as a known mismatch but the contract no "
                "longer declares it — delete the entry"
            )
            assert reason and reason.strip(), f"{key} is recorded with no reason"
            assert "OWNER:" in reason, (
                f"{key} names a defect but not who owns it — an unowned "
                "finding is a finding nobody fixes"
            )


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = _recipe_for(table, method, path, code)
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    response = perform(Call(method, path))
    assert response.status_code == code, (
        f"{method} {path}: expected the declared {code}, got "
        f"{response.status_code}: {response.content[:400]}"
    )

    body = response.json()
    errors = sorted(_validator(body_schema).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # An empty list validates against any item schema, so a collection must
    # actually carry a row for the check to have looked at anything — both the
    # bare arrays and the anchor-pagination envelope's ``items``.
    if expect_rows:
        rows = body if isinstance(body, list) else None
        if rows is None and isinstance(body, dict) and isinstance(body.get("items"), list):
            rows = body["items"]
        if rows is not None:
            assert rows, f"{method} {path}: the declared collection came back empty"
    return body


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if _recipe_for(EMPTY_STATE, method, path, code) is not None
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    if (method, path) in KNOWN_MISMATCHES_EMPTY:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES_EMPTY[(method, path)]}",
            )
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object, so every one of them must fail. If any passes, the validation
    in ``_drive`` is not reaching the received body and this whole file proves
    nothing.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path) not in KNOWN_MISMATCHES and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
