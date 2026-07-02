"""Render SA documentation from registered flows.

    python manage.py generate_flow_docs --out docs/flows
"""
from pathlib import Path

from django.core.management.base import BaseCommand

from stapel_core.flows import autodiscover_flows, flow_registry
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

    def handle(self, *args, **options):
        autodiscover_flows()
        flows = flow_registry.all()
        if not flows:
            self.stdout.write(self.style.WARNING("no flows registered"))
            return
        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        index = endpoint_index()
        for flow in flows:
            (out / f"{flow.id}.md").write_text(render_flow_markdown(flow, index))
        (out / "README.md").write_text(render_index_markdown(flows, index))
        (out / "flows.json").write_text(export_json(flows, index))
        self.stdout.write(self.style.SUCCESS(
            f"wrote {len(flows)} flow doc(s) to {out}/"
        ))
