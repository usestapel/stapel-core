"""CI gate: every endpoint documented via flows, every flow complete.

    python manage.py check_flows [--allow /internal/]

Exits non-zero on errors (warnings are printed but do not fail).
"""
import sys

from django.core.management.base import BaseCommand

from stapel_core.flows import autodiscover_flows
from stapel_core.flows.checks import check_flows


class Command(BaseCommand):
    help = "Verify flow documentation completeness (CI gate)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--allow", action="append", default=[],
            help="Extra path substring to exempt from flow coverage (repeatable)",
        )

    def handle(self, *args, **options):
        autodiscover_flows()
        issues = check_flows(extra_allowlist=tuple(options["allow"]))
        errors = [i for i in issues if i.level == "error"]
        for issue in issues:
            style = self.style.ERROR if issue.level == "error" else self.style.WARNING
            self.stdout.write(style(f"[{issue.level}] {issue.message}"))
        if errors:
            self.stdout.write(self.style.ERROR(f"{len(errors)} flow error(s)"))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS("flows OK"))
