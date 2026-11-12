"""Remove stored CDN URLs from attachments written before 0.12.0.

    python manage.py chat_strip_attachment_urls [--batch-size N] [--limit N] [--dry-run]

Not needed for correctness — reads drop stored URLs and resolve fresh ones —
but it leaves no expired or formerly public URL at rest. Walks the table in
primary-key batches, each its own short transaction; rerunnable. See
``attachment_url_strip.py``.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Rewrite chat message attachments that still carry CDN URLs into the "
        "stored reference form, in bounded batches."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows read per batch; each rewriting batch is one short transaction.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Stop after this many rewritten rows.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count the rows that would change, write nothing.",
        )

    def handle(self, *args, **options):
        from ...attachment_url_strip import strip_attachment_urls

        stats = strip_attachment_urls(
            batch_size=options["batch_size"],
            limit=options["limit"],
            dry_run=options["dry_run"],
        )
        verb = "would rewrite" if options["dry_run"] else "rewrote"
        self.stdout.write(
            f"chat_strip_attachment_urls: scanned {stats['scanned']} row(s), "
            f"{verb} {stats['rewritten']}."
        )
