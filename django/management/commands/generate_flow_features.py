"""Generate Gherkin ``.feature`` files + playwright-bdd step-defs from flows.

    python manage.py generate_flow_features --out features

flow-system.md §3 (wish #3): the flow is the source, the ``.feature`` is a
projection. One self-contained bundle per project language::

    features/
      README.md                       # links every language bundle
      en/
        <flow_id>.feature             # localized Gherkin (happy path)
        steps/flows.steps.ts          # playwright-bdd step library
        steps/fixtures.ts             # the `stapel` world (codegen client)
      ru/
        …

Each bundle is self-consistent: the step-def regexes are the resolved notes in
that language, so the suite runs in the project language. The output is
byte-stable (deterministic sorts, resolved i18n, no timestamps), so the
release-gate drift check (`generate_flow_features` + `git diff --exit-code`)
stays meaningful — a no-op regeneration is a no-op diff, the same discipline as
the SA-doc trees (`generate_project_docs`). ``--llm`` machine-translates keys
missing from catalogs/translate through the DOC_TRANSLATOR seam, content-hash
cached per language.

Runner: playwright-bdd (TS-first, the codegen typed client drives HTTP steps).
"""
from pathlib import Path

from django.core.management.base import BaseCommand

from stapel_core.flows import (
    autodiscover_flows,
    flow_registry,
    resolve_flow_texts,
    write_language_bundle,
)
from stapel_core.flows.docs import endpoint_index
from stapel_core.i18n import project_languages

#: Human-facing names for the top-level language index; falls back to the code.
LANGUAGE_NAMES = {"en": "English", "ru": "Русский"}


class Command(BaseCommand):
    help = ("Generate Gherkin .feature files + playwright-bdd step-defs "
            "(one bundle per project language) from registered flows.")

    def add_arguments(self, parser):
        parser.add_argument("--out", default="features", help="Output directory")
        parser.add_argument(
            "--languages", default="",
            help="Comma-separated language override (default: the project "
                 "languages — STAPEL_I18N['LOCALES'], unless STAPEL_FLOWS"
                 "['DOC_LANGUAGES'] is explicitly set).",
        )
        parser.add_argument(
            "--llm", action="store_true",
            help="Machine-translate keys missing from catalogs/translate via "
                 "the DOC_TRANSLATOR seam (content-hash cached per language).",
        )

    def handle(self, *args, **options):
        autodiscover_flows()
        flows = flow_registry.all()
        if not flows:
            self.stdout.write(self.style.WARNING("no flows registered"))
            return

        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        languages = [
            lg.strip() for lg in options["languages"].split(",") if lg.strip()
        ] or project_languages()

        index = endpoint_index()

        for lang in languages:
            bundle = out / lang
            bundle.mkdir(parents=True, exist_ok=True)  # the --llm cache lives here
            texts = resolve_flow_texts(
                flows, lang,
                llm=options["llm"],
                cache_path=(bundle / f"flow-i18n-cache.{lang}.json")
                if options["llm"] else None,
            )
            write_language_bundle(flows, index, bundle, lang, texts)

        (out / "README.md").write_text(self._language_index(languages))
        self.stdout.write(self.style.SUCCESS(
            f"wrote {len(flows)} flow(s) × {len(languages)} language(s) "
            f"({', '.join(languages)}) to {out}/"
        ))

    @staticmethod
    def _language_index(languages: list[str]) -> str:
        lines = ["# Flow features", "",
                 "Executable Gherkin (playwright-bdd), one bundle per language:",
                 ""]
        for lang in languages:
            name = LANGUAGE_NAMES.get(lang, lang)
            lines.append(f"- [{name}]({lang}/)")
        return "\n".join(lines) + "\n"
