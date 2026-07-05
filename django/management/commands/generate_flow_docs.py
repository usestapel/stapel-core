"""Render SA documentation from registered flows.

    python manage.py generate_flow_docs --out docs/flows

i18n (flow-system.md §2): ``--lang X`` renders the markdown with every
flow/step key resolved for language X — committed per-app catalogs
(``<app>/translations/flows.X.json``) → ``translate.resolve`` comm Function
→ (with ``--llm``) the DOC_TRANSLATOR seam → source literal. ``--llm`` uses
a content-hash cache next to the docs, so re-running without source changes
makes zero LLM calls and produces zero diff. ``flows.json`` is always the
language-agnostic artifact (keys + canonical literals + API bindings),
regardless of ``--lang``.
"""
from pathlib import Path

from django.core.management.base import BaseCommand

from stapel_core.flows import autodiscover_flows, flow_registry, resolve_flow_texts
from stapel_core.flows.docs import (
    endpoint_index,
    export_json,
    render_flow_markdown,
    render_index_markdown,
)


class Command(BaseCommand):
    help = "Generate markdown flow documentation (+ flows.json) from registered flows."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="docs/flows", help="Output directory")
        parser.add_argument(
            "--lang", default="",
            help="Resolve flow i18n keys for this language when rendering the "
                 "markdown (flows.json stays language-agnostic).",
        )
        parser.add_argument(
            "--llm", action="store_true",
            help="With --lang: machine-translate keys missing from catalogs/"
                 "translate via the DOC_TRANSLATOR seam (content-hash cached).",
        )
        parser.add_argument(
            "--llm-cache", default="",
            help="Cache file for --llm (default: <out>/flow-i18n-cache.<lang>.json). "
                 "Commit it — unchanged sources then cost zero LLM calls and zero diff.",
        )

    def handle(self, *args, **options):
        autodiscover_flows()
        flows = flow_registry.all()
        if not flows:
            self.stdout.write(self.style.WARNING("no flows registered"))
            return
        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        lang = options["lang"]
        texts = None
        if lang:
            cache_path = Path(options["llm_cache"]) if options["llm_cache"] \
                else out / f"flow-i18n-cache.{lang}.json"
            texts = resolve_flow_texts(
                flows, lang,
                llm=options["llm"],
                cache_path=cache_path if options["llm"] else None,
            )
        index = endpoint_index()
        for flow in flows:
            (out / f"{flow.id}.md").write_text(render_flow_markdown(flow, index, texts=texts))
        (out / "README.md").write_text(render_index_markdown(flows, index, texts=texts))
        (out / "flows.json").write_text(export_json(flows, index))
        self.stdout.write(self.style.SUCCESS(
            f"wrote {len(flows)} flow doc(s) to {out}/"
            + (f" (lang={lang})" if lang else "")
        ))
