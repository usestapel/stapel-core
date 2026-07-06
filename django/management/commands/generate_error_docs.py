"""Emit the human-readable error reference ``docs/errors.<lang>.md``.

    python manage.py generate_error_docs [--lang ru] [--out docs] \
        [--translations translations]

i18n-shipping.md §4: the readable companion of the machine ``errors.json``
artifact. One table per language (``--lang``, or every project language),
byte-stable so a no-op regen is a no-op diff — gate it with the same
regenerate-and-diff test as the flow docs. The en table is the registry canon;
a localized table joins the registry with ``translations/errors.<lang>.json``.
Force-imports every ``errors`` module first (like ``generate_error_keys``) so
the registry is complete.
"""
from importlib import import_module
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.module_loading import autodiscover_modules

from stapel_core.i18n import project_languages
from stapel_core.i18n.conf import i18n_settings
from stapel_core.i18n.errordocs import build_error_docs, error_docs_filename

_CORE_ERROR_MODULES = (
    "stapel_core.verification.errors",
    "stapel_core.django.captcha",
)


class Command(BaseCommand):
    help = "Emit docs/errors.<lang>.md — the human-readable error reference."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="docs", help="Output directory (default: docs).")
        parser.add_argument(
            "--lang", default="",
            help="Single language (default: every project language).",
        )
        parser.add_argument(
            "--translations", default="translations",
            help="Directory holding errors.<lang>.json for localized tables.",
        )

    def handle(self, *args, **options):
        autodiscover_modules("errors")
        for mod in list(_CORE_ERROR_MODULES) + list(
            getattr(settings, "STAPEL_ERROR_MODULES", [])
        ):
            try:
                import_module(mod)
            except ImportError:
                continue

        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        languages = [options["lang"]] if options["lang"] else project_languages()
        source_language = i18n_settings.SOURCE_LANGUAGE

        for lang in languages:
            md = build_error_docs(
                lang,
                translations_dir=options["translations"],
                source_language=source_language,
            )
            (out / error_docs_filename(lang)).write_text(md, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"wrote errors reference for {', '.join(languages)} to {out}/"))
