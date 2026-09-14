"""Claim CDN media for messages sent before claim-on-send (0.9.2).

Every send from 0.9.2 on publishes its claim
(``services._schedule_message_cdn_ref_sync``), but a message already in the
table publishes nothing — no row changes, so nothing announces one. Those
attachments are still zero-ref on the CDN side, which means stapel-cdn's
hourly ``sweep_unclaimed`` will reap them, bytes and row, once their unclaimed
TTL runs out. This module is the one-time (or rerunnable) pass that claims
them before that happens.

Additive by construction: every event carries ``old_hashes=[]``, so
``apply_ref_sync``'s ``to_remove`` set is empty and a ref that is already
claimed is left exactly as it is. A rerun, or a run over rows the send path
already claimed, adds nothing and releases nothing.

Tombstones are skipped on purpose. ``deleted_at`` means the row released its
refs (``services.delete_message``) or was erased (``erase_user_messages``);
re-claiming there would undo a release the module performed deliberately and
keep media alive that a user asked to be rid of.

Graceful the same way the send-path sync is: a failed bus publish is counted
as ``failed`` and the pass keeps going — rerun once the broker is reachable.
"""
from __future__ import annotations

import logging

from .models import Message
from .services import CDN_ENTITY_TYPE, CDN_SERVICE, _message_cdn_refs

logger = logging.getLogger(__name__)


def backfill_cdn_refs(*, limit: int | None = None, dry_run: bool = False) -> dict:
    """Publish an additive claim for every live message that carries refs.

    ``limit`` bounds the number of candidate rows (rows that actually carry
    refs) — for running the pass in slices on a large table.

    Returns ``{"candidates": int, "published": int, "failed": int}``.
    """
    from stapel_core.django.cdn.ref_sync import sync_cdn_refs

    # Filtered in Python rather than by a JSONField predicate: `attachments`
    # is a JSON list and "is it empty" is spelled differently on every
    # backend this module supports. One indexed pass, `.only()` so the body
    # never leaves the database.
    qs = (
        Message.objects.filter(deleted_at__isnull=True)
        .order_by("pk")
        .only("id", "attachments")
    )

    stats = {"candidates": 0, "published": 0, "failed": 0}
    for message in qs.iterator(chunk_size=500):
        refs = _message_cdn_refs(message.attachments)
        if not refs:
            continue
        stats["candidates"] += 1
        if not dry_run:
            result = sync_cdn_refs(
                CDN_SERVICE, CDN_ENTITY_TYPE, str(message.id), [], sorted(refs)
            )
            if result is None or getattr(result, "ok", False):
                stats["published"] += 1
            else:
                stats["failed"] += 1
        if limit is not None and stats["candidates"] >= limit:
            break
    return stats
