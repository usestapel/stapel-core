"""Generate/refresh a localized catalog with provenance (i18n-shipping.md §5).

    python manage.py translate_catalogs --domain errors --lang ru [--llm] \
        [--seed FILE] [--seed-label LABEL] [--app LABEL | --out DIR] \
        [--approve KEY … | --approve-all]

The write-time sister of ``generate_flow_docs``. Materializes
``<out>/<domain>.<lang>.json`` (flat ``{code|key: text}``, byte-stable) from
the domain's canonical en source, recording provenance in ``<out>/.state.json``:

* a key already fresh (source hash matches ``.state.json``) is kept untouched;
* ``--seed FILE`` (a curated corpus — e.g. the stapel-translate builtin
  fixtures exported via ``stapel-i18n-seed``) supplies values marked
  ``origin: seed:<label>`` — cheap, but machine-made and therefore UNREVIEWED;
* ``--llm`` fills the remainder through the ``STAPEL_I18N["TRANSLATOR"]`` seam,
  content-hash cached (unchanged sources ⇒ zero LLM calls, zero diff), marked
  ``origin: llm`` (machine, unreviewed — the gate's W-counter);
* ``--approve KEY …`` / ``--approve-all`` flips reviewed keys to
  ``origin: human`` — review is a state transition, not hand-editing JSON, and
  it is the only thing that clears the unreviewed counter.

Keys nothing filled stay absent and fail ``check_translation_catalogs``.

The output directory is RESOLVED, not assumed: catalogs are discovered by
walking the package directories of INSTALLED_APPS, so a relative ``--out``
against a service root writes a file nothing will ever read. Default: the app
package this is run from; ``--app LABEL`` names one explicitly; an explicit
``--out`` that no loader would read is refused.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from stapel_core.i18n import dump_catalog, source_texts
from stapel_core.i18n.catalogs import (
    CatalogDirError,
    load_catalog_file,
    resolve_catalog_dir,
)
from stapel_core.i18n.conf import i18n_settings
from stapel_core.i18n.translate import translate_catalog


class Command(BaseCommand):
    help = "Generate/refresh a localized <domain>.<lang>.json catalog + provenance."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Catalog domain (errors, flows).")
        parser.add_argument("--lang", required=True, help="Target language, e.g. ru.")
        parser.add_argument(
            "--app", default=None, metavar="LABEL",
            help="Write into this installed app's translations/ directory.",
        )
        parser.add_argument(
            "--out", default=None,
            help="Catalog directory. Must be an installed app package's "
                 "translations/ (or an EXTRA_CATALOG_DIRS one) — anywhere else "
                 "is refused, because the loader would never read it. "
                 "Default: the app package the command runs from.",
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

        # .get: the handler is also driven directly (the core is not an
        # installed app in its own test config), so a caller predating --app
        # keeps working — and is still routed through the resolver.
        try:
            out_dir = resolve_catalog_dir(options.get("out"), app=options.get("app"))
        except CatalogDirError as exc:
            raise CommandError(str(exc))

        seed = None
        if options["seed"]:
            seed = load_catalog_file(Path(options["seed"]))
            if not seed:
                self.stdout.write(self.style.WARNING(
                    f"seed file {options['seed']} empty or unreadable — ignored"))

        result = translate_catalog(
            domain, lang, out_dir,
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
            f"{domain}/{lang} → {out_dir}: kept {result.kept}, "
            f"seeded {result.seeded}, translated {result.translated}, "
            f"imported {result.imported}, approved {result.approved}, "
            f"missing {len(result.missing)}"
            + ("" if result.written else " (no change)")
        ))
        if result.unreviewed:
            self.stdout.write(self.style.WARNING(
                f"  {result.unreviewed} value(s) written unreviewed "
                f"(seed/llm/imported are all machine provenance) — a human "
                f"clears them with --approve"))
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
