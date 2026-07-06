"""CI gate: shipped catalogs cover the canon, are fresh, and preserve params.

    python manage.py check_translation_catalogs --domain errors [--out translations] \
        [--languages ru,es] [--strict]

i18n-shipping.md §5. Errors (missing / stale / params-mismatch / not
byte-stable) fail the build. Unreviewed (``origin: llm`` / unknown) values are
a **counter**, printed but non-blocking — unless ``--strict`` (open question
#3: when the ru pass is reviewed, flip the switch). The module pytest wraps
this via :func:`stapel_core.i18n.check_translation_catalogs`.
"""
import sys

from django.core.management.base import BaseCommand, CommandError

from stapel_core.i18n import (
    check_translation_catalogs,
    project_languages,
    source_texts,
    summarize,
)
from stapel_core.i18n.conf import i18n_settings


class Command(BaseCommand):
    help = "Verify localized catalogs cover/track the canon (CI gate)."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Catalog domain (errors, flows).")
        parser.add_argument("--out", default="translations", help="Catalog directory.")
        parser.add_argument(
            "--languages", default="",
            help="Comma-separated languages to check (default: project languages).",
        )
        parser.add_argument(
            "--strict", action="store_true",
            help="Also fail on unreviewed (origin: llm/unknown) values.",
        )

    def handle(self, *args, **options):
        domain = options["domain"]
        try:
            source = source_texts(domain)
        except ValueError as exc:
            raise CommandError(str(exc))
        languages = [
            lg.strip() for lg in options["languages"].split(",") if lg.strip()
        ] or project_languages()

        issues = check_translation_catalogs(
            domain, options["out"],
            source_texts=source,
            languages=languages,
            source_language=i18n_settings.SOURCE_LANGUAGE,
        )
        errors, warnings = summarize(issues)
        for issue in issues:
            style = self.style.ERROR if issue.level == "error" else self.style.WARNING
            self.stdout.write(style(f"[{issue.level}:{issue.code}] {issue.message}"))

        unreviewed = sum(1 for i in issues if i.code == "unreviewed")
        if unreviewed:
            self.stdout.write(self.style.WARNING(
                f"{unreviewed} unreviewed value(s) (origin: llm/unknown) — "
                "review with `translate_catalogs --approve`"))

        fatal = errors + (warnings if options["strict"] else 0)
        if fatal:
            self.stdout.write(self.style.ERROR(
                f"{errors} error(s), {warnings} warning(s) in {domain} catalogs"
                + (" (strict)" if options["strict"] else "")))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS(
            f"{domain} catalogs OK ({warnings} warning(s))"))
