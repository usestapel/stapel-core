"""Run the project's declared seeds: ``python manage.py run_seeds``.

A seed is a management command that loads reference or demo data. A project
names its seeds in ``settings.STAPEL_SEEDS`` (a list of command names, run in
order); the generated ``bootstrap.sh`` calls this command as an ``optional``
step, so a seed's failure degrades the boot and names the seed instead of
taking the service down.

Every seed must be idempotent: bootstrap runs it on every start, so a second
run must leave the data as the first one did.

Usage:
    python manage.py run_seeds            # every seed in STAPEL_SEEDS
    python manage.py run_seeds --list     # print the declared seeds
"""

from django.conf import settings
from django.core.management import call_command, get_commands
from django.core.management.base import BaseCommand, CommandError


def declared_seeds() -> list[str]:
    """``settings.STAPEL_SEEDS`` as a list of command names (``[]`` when unset)."""
    seeds = getattr(settings, "STAPEL_SEEDS", None) or []
    if isinstance(seeds, str) or not all(isinstance(s, str) and s for s in seeds):
        raise CommandError(
            f"STAPEL_SEEDS must be a list of management command names, got {seeds!r}"
        )
    return list(seeds)


class Command(BaseCommand):
    help = "Run the idempotent seed commands named in settings.STAPEL_SEEDS, in order"

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Print the declared seeds and exit"
        )

    def handle(self, *args, **options):
        del args
        seeds = declared_seeds()
        if options.get("list"):
            for name in seeds:
                self.stdout.write(name)
            return
        if not seeds:
            self.stdout.write("run_seeds: STAPEL_SEEDS is empty, nothing to run")
            return
        # Every name is checked before anything runs: a typo in the last seed
        # must not leave the first ones applied and the cause buried in a log.
        missing = [name for name in seeds if name not in get_commands()]
        if missing:
            raise CommandError(
                "run_seeds: STAPEL_SEEDS names no installed management command: "
                + ", ".join(missing)
            )
        for name in seeds:
            self.stdout.write(f"run_seeds: {name}")
            try:
                call_command(name, stdout=self.stdout, stderr=self.stderr)
            except Exception as exc:
                raise CommandError(f"run_seeds: seed {name!r} failed: {exc}") from exc
        self.stdout.write(f"run_seeds: {len(seeds)} seed(s) done")
