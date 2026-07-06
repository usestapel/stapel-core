"""Assemble the project's flow documentation as bilingual doc trees.

    python manage.py generate_project_docs --out docs/flows

flow-system.md §4 / attributes-admin-ui.md решение 5: one command, one tree
per ``STAPEL_FLOWS["DOC_LANGUAGES"]`` language (``["en", "ru"]`` by default),
generated from the single language-agnostic ``flows.json``. Layout::

    docs/flows/
      flows.json            # language-agnostic machine artifact (once)
      README.md             # links every language tree
      en/  README.md + <flow_id>.md …
      ru/  README.md + <flow_id>.md …

The output is byte-stable (deterministic sorts, no timestamps), so the
release-gate drift check (`generate_project_docs` + `git diff --exit-code`)
stays meaningful — regeneration without source changes produces zero diff.
``--llm`` machine-translates keys missing from catalogs/translate through
the DOC_TRANSLATOR seam, content-hash cached next to each language tree.
"""
from pathlib import Path

from django.core.management.base import BaseCommand

from stapel_core.flows import autodiscover_flows, flow_registry, resolve_flow_texts
from stapel_core.flows.docs import endpoint_index, export_json, get_flow_doc_renderer
from stapel_core.i18n import project_languages

#: Human-facing names for the top-level language index; falls back to the code.
LANGUAGE_NAMES = {"en": "English", "ru": "Русский"}


class Command(BaseCommand):
    help = "Generate the project's bilingual flow doc trees (one per DOC_LANGUAGES)."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="docs/flows", help="Output directory")
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
        renderer = get_flow_doc_renderer()

        # Language-agnostic artifact — written once, identical regardless of
        # the languages requested.
        (out / "flows.json").write_text(export_json(flows, index))

        for lang in languages:
            tree = out / lang
            tree.mkdir(parents=True, exist_ok=True)
            texts = resolve_flow_texts(
                flows, lang,
                llm=options["llm"],
                cache_path=(tree / f"flow-i18n-cache.{lang}.json")
                if options["llm"] else None,
            )
            for flow in flows:
                (tree / f"{flow.id}.md").write_text(
                    renderer.render_flow(flow, index, texts, lang)
                )
            (tree / "README.md").write_text(
                renderer.render_index(flows, index, texts, lang)
            )

        (out / "README.md").write_text(self._language_index(languages))
        self.stdout.write(self.style.SUCCESS(
            f"wrote {len(flows)} flow(s) × {len(languages)} language(s) "
            f"({', '.join(languages)}) to {out}/"
        ))

    @staticmethod
    def _language_index(languages: list[str]) -> str:
        lines = ["# Flows", ""]
        for lang in languages:
            name = LANGUAGE_NAMES.get(lang, lang)
            lines.append(f"- [{name}]({lang}/README.md)")
        return "\n".join(lines) + "\n"
