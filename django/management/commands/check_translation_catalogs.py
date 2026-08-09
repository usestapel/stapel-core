"""CI gate: shipped catalogs cover the canon, are fresh, and preserve params.

    python manage.py check_translation_catalogs --domain errors \
        [--app LABEL | --out DIR] [--languages ru,es] [--strict]

i18n-shipping.md §5. Errors (missing / foreign / stale / params-mismatch / not
byte-stable) fail the build. Coverage is scoped to the keys the gated app
**owns**; a key another package owns and already ships in this language is a
``foreign`` error unless the sidecar declares the override
(``translate_catalogs --declare-override``). Unreviewed values — anything no human approved:
``origin: llm``, ``seed:<label>``, ``imported``, or no sidecar row — are a
**counter**, printed but non-blocking, unless ``--strict`` (open question #3:
when a locale pass is reviewed, flip the switch). The module pytest wraps this
via :func:`stapel_core.i18n.check_translation_catalogs`.

The directory is resolved the same way ``translate_catalogs`` resolves it —
gating a directory the loader cannot read is as useless as writing into one.
"""
import sys

from django.core.management.base import BaseCommand, CommandError

from stapel_core.i18n import (
    check_translation_catalogs,
    project_languages,
    source_texts,
    summarize,
)
from stapel_core.i18n.catalogs import CatalogDirError, resolve_catalog_dir
from stapel_core.i18n.conf import i18n_settings


class Command(BaseCommand):
    help = "Verify localized catalogs cover/track the canon (CI gate)."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Catalog domain (errors, flows).")
        parser.add_argument(
            "--app", default=None, metavar="LABEL",
            help="Gate this installed app's translations/ directory.",
        )
        parser.add_argument(
            "--out", default=None,
            help="Catalog directory (must be one the loader reads). "
                 "Default: the app package the command runs from.",
        )
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
        try:
            out_dir = resolve_catalog_dir(options.get("out"), app=options.get("app"))
        except CatalogDirError as exc:
            raise CommandError(str(exc))

        issues = check_translation_catalogs(
            domain, out_dir,
            source_texts=source,
            languages=languages,
            source_language=i18n_settings.SOURCE_LANGUAGE,
            undeclared_overrides=i18n_settings.UNDECLARED_OVERRIDES,
        )
        errors, warnings = summarize(issues)
        for issue in issues:
            style = self.style.ERROR if issue.level == "error" else self.style.WARNING
            self.stdout.write(style(f"[{issue.level}:{issue.code}] {issue.message}"))

        unreviewed = sum(1 for i in issues if i.code == "unreviewed")
        if unreviewed:
            self.stdout.write(self.style.WARNING(
                f"{unreviewed} unreviewed value(s) in {out_dir} — no human has "
                "approved them (llm/seed/imported/unknown provenance all "
                "count); review with `translate_catalogs --approve`"))

        fatal = errors + (warnings if options["strict"] else 0)
        if fatal:
            self.stdout.write(self.style.ERROR(
                f"{errors} error(s), {warnings} warning(s) in {domain} catalogs"
                + (" (strict)" if options["strict"] else "")))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS(
            f"{domain} catalogs OK ({warnings} warning(s))"))
