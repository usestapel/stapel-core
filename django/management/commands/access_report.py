"""``access_report`` — audit surface of the staff mandate (admin-suite §3.8).

Prints the role × model × operation matrix computed from the ``@access``
declarations and ``STAPEL_ACCESS`` (A1 — the report *is* the effective
policy, there is no stored state that could diverge), every manual DAC
grant above a staff user's mandate (A4), and the models running on the
implicit standard declaration. In microservices, run per service.
"""
import json

from django.core.management.base import BaseCommand

from stapel_core.access.report import build_report, render_text


class Command(BaseCommand):
    help = (
        "Staff-mandate audit: role×model×operation matrix, DAC grants above "
        "the mandate, models without an @access declaration."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit the report as JSON instead of text.",
        )

    def handle(self, *args, **options):
        report = build_report()
        if options.get("as_json"):
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        else:
            self.stdout.write(render_text(report))
