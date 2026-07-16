"""Regenerate PRESENTERS.MD — the swappable-class/presenter catalog (§55 §4).

    python manage.py presenter_catalog [--out PRESENTERS.MD] [--check]

Pure introspection (``stapel_core.django.api.catalog``): every installed
app's ``presenters`` module is imported, then the ``STAPEL_SWAP``
declaration registry and the Presenter registry are rendered into one
markdown catalog — swap key -> default class -> DTO -> fields. ``--check``
compares against the existing file instead of writing (the REL-freshness
gate: a catalog that drifts from code fails, exit 1).
"""
import sys
from pathlib import Path

from django.core.management.base import BaseCommand

from stapel_core.django.api.catalog import (
    PRESENTERS_MD,
    presenter_catalog,
    render_presenters_md,
)


class Command(BaseCommand):
    help = "Regenerate PRESENTERS.MD from the swap/presenter registries (or --check freshness)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--out", default=PRESENTERS_MD,
            help=f"Output file (default: {PRESENTERS_MD} in the cwd).",
        )
        parser.add_argument(
            "--check", action="store_true",
            help="Don't write; exit 1 if the existing file differs from the "
                 "regenerated content (release freshness gate).",
        )

    def handle(self, *args, **options):
        entries = presenter_catalog()
        text = render_presenters_md(entries)
        out = Path(options["out"])

        if options["check"]:
            current = out.read_text(encoding="utf-8") if out.is_file() else None
            if current != text:
                self.stdout.write(self.style.ERROR(
                    f"{out} is stale (or missing) — regenerate with "
                    f"`manage.py presenter_catalog --out {out}`"
                ))
                sys.exit(1)
            self.stdout.write(self.style.SUCCESS(f"{out} is fresh ({len(entries)} presenter(s))"))
            return

        out.write_text(text, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"wrote {out} ({len(entries)} presenter(s), "
            f"{sum(len(e.fields) for e in entries)} field(s))"
        ))
