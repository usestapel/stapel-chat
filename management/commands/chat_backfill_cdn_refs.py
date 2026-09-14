"""Claim CDN media for messages sent before claim-on-send (0.9.2).

    python manage.py chat_backfill_cdn_refs [--limit N] [--dry-run]

A message already in the table publishes no claim of its own — nothing about
it changes, so nothing announces one — and its attachments stay zero-ref on
the CDN side, where ``sweep_unclaimed`` reaps them once the unclaimed TTL runs
out. This command publishes an ADDITIVE claim (``old_hashes=[]``) for every
live message that references media: idempotent and rerunnable by construction
(nothing is ever released), and a failed bus publish is counted, not raised —
rerun once the broker is up. See ``cdn_refs_backfill.py``.

Verify with stapel-cdn's own diagnostic rather than by arithmetic on
``unreferenced_since``: ``manage.py cdn_sweep_unclaimed --dry-run`` counts the
objects the next sweep would actually take.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Publish additive CDN ref claims (stapel.cdn.ref-sync) for every live "
        "chat message's attachments, for rows that predate claim-on-send."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Stop after this many candidate rows (rows that carry refs). "
                "For running the backfill in bounded slices on a large table."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count candidates and report, publish nothing.",
        )

    def handle(self, *args, **options):
        from ...cdn_refs_backfill import backfill_cdn_refs

        stats = backfill_cdn_refs(limit=options["limit"], dry_run=options["dry_run"])
        dry = options["dry_run"]
        verb = "would claim" if dry else "claimed"
        count = stats["candidates"] if dry else stats["published"]
        self.stdout.write(
            f"chat_backfill_cdn_refs: {stats['candidates']} candidate(s), "
            f"{verb} {count} claim set(s), {stats['failed']} failed to publish."
        )
        if stats["failed"] and not stats["published"]:
            self.stdout.write(
                self.style.WARNING(
                    "Every publish failed — the bus is unreachable. Nothing "
                    "was claimed; rerun once the broker is up, and do NOT let "
                    "the CDN sweeper run before this pass succeeds."
                )
            )
