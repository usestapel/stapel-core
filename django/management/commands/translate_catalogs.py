"""Generate/refresh a localized catalog with provenance (i18n-shipping.md §5).

    python manage.py translate_catalogs --domain errors --lang ru [--llm] \
        [--seed FILE] [--seed-label LABEL] [--out translations] \
        [--approve KEY … | --approve-all]

The write-time sister of ``generate_flow_docs``. Materializes
``<out>/<domain>.<lang>.json`` (flat ``{code|key: text}``, byte-stable) from
the domain's canonical en source, recording provenance in ``<out>/.state.json``:

* a key already fresh (source hash matches ``.state.json``) is kept untouched;
* ``--seed FILE`` (a curated corpus — e.g. the stapel-translate builtin
  fixtures exported via ``stapel-i18n-seed``) supplies values marked
  ``origin: seed:<label>``;
* ``--llm`` fills the remainder through the ``STAPEL_I18N["TRANSLATOR"]`` seam,
  content-hash cached (unchanged sources ⇒ zero LLM calls, zero diff), marked
  ``origin: llm`` (machine, unreviewed — the gate's W-counter);
* ``--approve KEY …`` / ``--approve-all`` flips reviewed keys to
  ``origin: human`` — review is a state transition, not hand-editing JSON.

Keys nothing filled stay absent and fail ``check_translation_catalogs``.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from stapel_core.i18n import dump_catalog, source_texts
from stapel_core.i18n.catalogs import load_catalog_file
from stapel_core.i18n.conf import i18n_settings
from stapel_core.i18n.translate import translate_catalog


class Command(BaseCommand):
    help = "Generate/refresh a localized <domain>.<lang>.json catalog + provenance."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Catalog domain (errors, flows).")
        parser.add_argument("--lang", required=True, help="Target language, e.g. ru.")
        parser.add_argument(
            "--out", default="translations",
            help="Catalog directory (default: translations).",
        )
        parser.add_argument(
            "--seed", default="",
            help="A flat {key: text} JSON seed (curated corpus). Keys outside "
                 "the domain registry are ignored.",
        )
        parser.add_argument(
            "--seed-label", default="seed",
            help="Provenance label for seeded values (origin: seed:<label>).",
        )
        parser.add_argument(
            "--llm", action="store_true",
            help="Machine-translate the remainder via the STAPEL_I18N translator "
                 "seam (content-hash cached).",
        )
        parser.add_argument(
            "--approve", nargs="*", default=None, metavar="KEY",
            help="Mark these keys reviewed (origin: human) without retranslating.",
        )
        parser.add_argument(
            "--approve-all", action="store_true",
            help="Mark every present key reviewed (origin: human).",
        )

    def handle(self, *args, **options):
        domain = options["domain"]
        lang = options["lang"]
        try:
            source = source_texts(domain)
        except ValueError as exc:
            raise CommandError(str(exc))

        seed = None
        if options["seed"]:
            seed = load_catalog_file(Path(options["seed"]))
            if not seed:
                self.stdout.write(self.style.WARNING(
                    f"seed file {options['seed']} empty or unreadable — ignored"))

        result = translate_catalog(
            domain, lang, options["out"],
            source_texts=source,
            source_language=i18n_settings.SOURCE_LANGUAGE,
            seed=seed,
            seed_label=options["seed_label"],
            llm=options["llm"],
            approve=options["approve"],
            approve_all=options["approve_all"],
        )

        style = self.style.SUCCESS if not result.missing else self.style.WARNING
        self.stdout.write(style(
            f"{domain}/{lang}: kept {result.kept}, seeded {result.seeded}, "
            f"translated {result.translated}, approved {result.approved}, "
            f"missing {len(result.missing)}"
            + ("" if result.written else " (no change)")
        ))
        if result.missing:
            preview = ", ".join(result.missing[:8])
            more = "" if len(result.missing) <= 8 else f" (+{len(result.missing) - 8})"
            self.stdout.write(self.style.WARNING(
                f"  missing (will fail the gate): {preview}{more} — "
                f"pass --seed and/or --llm"))
        # Nudge byte-stability drift for a caller who edited the JSON by hand.
        if result.catalog_path and result.catalog_path.is_file():
            catalog = load_catalog_file(result.catalog_path)
            if catalog and result.catalog_path.read_text(encoding="utf-8") != dump_catalog(catalog):
                self.stdout.write(self.style.WARNING(
                    "  note: catalog was re-normalised to byte-stable form"))
