"""Remove stored URLs from attachments written before 0.12.0.

Before 0.12.0 a message row kept the CDN's describe snapshot, URLs included.
Reads no longer serve those URLs (:func:`stapel_chat.attachments.resolve_messages`
drops them and resolves fresh ones), so old rows already read correctly; this
pass only removes the dead URLs at rest.

Bounded by design, so it never holds a lock on a large table: rows are walked
in primary-key order in batches of ``batch_size``, and each batch that needs a
rewrite is its own short transaction that locks only the rows it changes and
recomputes them from the locked value — a message deleted or erased while the
pass runs keeps its empty attachments. Rerunnable: a row already in reference
form is not touched. ``rev_seq`` does not move — nothing a reader sees changes.
"""
from __future__ import annotations

from django.db import transaction

from .attachments import reference_form
from .models import Message


def _stripped(attachments):
    """The reference form of a stored list, or ``None`` when it already is."""
    if not isinstance(attachments, list) or not attachments:
        return None
    out = [reference_form(a) for a in attachments]
    out = [a for a in out if a]
    return None if out == attachments else out


def strip_attachment_urls(
    *, batch_size: int = 500, limit: int | None = None, dry_run: bool = False
) -> dict:
    """Rewrite rows whose attachments still carry URLs.

    Returns ``{"scanned": int, "rewritten": int}`` (``rewritten`` counts the
    rows that would change under ``dry_run``). ``limit`` stops after that many
    rewrites, for running the pass in slices.
    """
    batch_size = max(1, int(batch_size))
    stats = {"scanned": 0, "rewritten": 0}
    last_pk = None
    while True:
        qs = Message.objects.filter(deleted_at__isnull=True).order_by("pk")
        if last_pk is not None:
            qs = qs.filter(pk__gt=last_pk)
        batch = list(qs.values_list("pk", "attachments")[:batch_size])
        if not batch:
            break
        last_pk = batch[-1][0]
        stats["scanned"] += len(batch)
        due = [pk for pk, attachments in batch if _stripped(attachments) is not None]
        if limit is not None:
            due = due[: max(0, limit - stats["rewritten"])]
        if due and not dry_run:
            with transaction.atomic():
                locked = (
                    Message.objects.select_for_update()
                    .filter(pk__in=due)
                    .values_list("pk", "attachments")
                )
                for pk, attachments in locked:
                    new = _stripped(attachments)
                    if new is not None:
                        Message.objects.filter(pk=pk).update(attachments=new)
        stats["rewritten"] += len(due)
        if limit is not None and stats["rewritten"] >= limit:
            break
    return stats


__all__ = ["strip_attachment_urls"]
