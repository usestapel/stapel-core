"""Pre-upgrade check: does THIS deployment match what the code expects?

Every failure of the ironmemo stand upgrade (2026-07-25/26) was the same
shape — a library arrived carrying an implicit requirement about the
deployment it lands in, nothing declared it, and it was discovered by
crashing in production:

  * a renamed app label met a database that had migrated under the old one
    (`relation … already exists`, migrate dead for the whole fleet);
  * an opt-in app needed a sibling Django app nobody had listed;
  * the event-bus default flipped to in-process, so every consumer worker
    silently no-op'd and restart-looped for weeks;
  * a comm transport needed an optional dependency the service never
    installed.

This command asks those questions BEFORE anything is touched, against the
real settings, the real database and the real installed packages. It
changes nothing. Exit code 1 means "do not deploy yet", and every finding
carries the fix.

    python manage.py stapel_preflight
    python manage.py stapel_preflight --json     # for a release harness/UI

Findings are data, not prose, so a non-technical operator can be shown a
list of decisions instead of a traceback.
"""
from __future__ import annotations

import importlib
import json as jsonlib
from dataclasses import asdict, dataclass, field

from django.core.management.base import BaseCommand

ERROR = "error"
WARNING = "warning"
INFO = "info"


@dataclass
class Finding:
    level: str
    code: str
    message: str
    fix: str = ""
    context: dict = field(default_factory=dict)


def _table_names() -> set[str]:
    from django.db import connection

    return set(connection.introspection.table_names())


def check_migration_adoption() -> list[Finding]:
    """An unapplied INITIAL migration whose table already exists.

    That is the app-rename/extraction hazard: the rows were created under
    another app label, so Django wants to create them again. A migration
    written to adopt (see stapel-core 0.14.1) passes; a plain CreateModel
    aborts the deploy at boot.
    """
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection, ignore_no_migrations=True)
    applied = loader.applied_migrations
    tables = _table_names()
    findings: list[Finding] = []

    for (app_label, name), migration in loader.disk_migrations.items():
        if (app_label, name) in applied or not getattr(migration, "initial", False):
            continue
        for operation in migration.operations:
            for table in _created_tables(operation):
                if table in tables:
                    findings.append(Finding(
                        level=ERROR,
                        code="preflight.E001",
                        message=(
                            f"{app_label}.{name} is unapplied but its table "
                            f"'{table}' already exists — migrate will abort."
                        ),
                        fix=(
                            "The table belongs to an app that was renamed or "
                            "extracted. Either ship an adoption-capable initial "
                            "migration (create the table only when absent), or "
                            "run `migrate --fake-initial` once for this app "
                            "after verifying the schema matches."
                        ),
                        context={"app": app_label, "migration": name, "table": table},
                    ))
    return findings


def _created_tables(operation) -> list[str]:
    """Tables an operation would CREATE (recursing into wrappers)."""
    from django.db import migrations as m

    if isinstance(operation, m.SeparateDatabaseAndState):
        tables = []
        for inner in operation.database_operations:
            tables.extend(_created_tables(inner))
        return tables
    if isinstance(operation, m.CreateModel):
        options = operation.options or {}
        table = options.get("db_table")
        if table:
            return [table]
        return [f"{operation.name.lower()}"]  # best effort; app_label unknown here
    return []


def check_bus_topology() -> list[Finding]:
    """An in-process bus cannot serve separate consumer processes."""
    from django.conf import settings

    backend = getattr(settings, "STAPEL_BUS_BACKEND", "")
    comm = getattr(settings, "STAPEL_COMM", {}) or {}
    if "memory" not in backend.lower():
        return []
    if comm.get("ACTION_TRANSPORT") != "bus":
        return []
    return [Finding(
        level=ERROR,
        code="preflight.E002",
        message=(
            "STAPEL_COMM['ACTION_TRANSPORT'] is 'bus' but STAPEL_BUS_BACKEND "
            f"is in-process ({backend or 'default MemoryBus'}) — consumer "
            "processes will drain an empty queue, exit, and restart forever "
            "while no cross-service event is delivered."
        ),
        fix=(
            "Point STAPEL_BUS_BACKEND at a broker backend, e.g. "
            "'stapel_core.bus.backends.kafka.KafkaBus' with "
            "KAFKA_BOOTSTRAP_SERVERS set."
        ),
        context={"backend": backend},
    )]


TRANSPORT_MODULES = {"nats": "nats", "kafka": "confluent_kafka"}


def check_transport_dependencies() -> list[Finding]:
    """A configured transport whose client library is not installed."""
    from django.conf import settings

    findings: list[Finding] = []
    comm = getattr(settings, "STAPEL_COMM", {}) or {}
    configured = {
        "FUNCTION_TRANSPORT": comm.get("FUNCTION_TRANSPORT", ""),
        "ACTION_TRANSPORT": comm.get("ACTION_TRANSPORT", ""),
    }
    backend = getattr(settings, "STAPEL_BUS_BACKEND", "")
    if "kafka" in backend.lower():
        configured["STAPEL_BUS_BACKEND"] = "kafka"

    for setting, value in configured.items():
        module = TRANSPORT_MODULES.get(str(value).lower())
        if not module:
            continue
        try:
            importlib.import_module(module)
        except ImportError:
            findings.append(Finding(
                level=ERROR,
                code="preflight.E003",
                message=(
                    f"{setting}={value} needs the '{module}' package, which is "
                    "not installed — the worker will crash on startup."
                ),
                fix=f"Install the matching extra (e.g. stapel-core[{value}]).",
                context={"setting": setting, "transport": value, "module": module},
            ))
    return findings


def run_checks_impl():
    """Seam: Django's `run_checks`, named so a test can inject an Error
    instead of depending on some other test module having registered a
    check-tripping model (which is how 0.15.0 shipped on a red test)."""
    from django.core.checks import run_checks

    return run_checks()


def check_system_checks() -> list[Finding]:
    """Surface Django's own system-check ERRORS as preflight findings.

    Deploy scripts run `migrate`, which runs checks — but only once the
    container is already starting, i.e. after the point of no return.
    """
    from django.core.checks import Error as CheckError

    findings = []
    for issue in run_checks_impl():
        if isinstance(issue, CheckError) or getattr(issue, "level", 0) >= 40:
            findings.append(Finding(
                level=ERROR,
                code=f"check.{issue.id or 'unknown'}",
                message=str(issue.msg),
                fix=str(getattr(issue, "hint", "") or ""),
            ))
    return findings


def check_pending_migrations() -> list[Finding]:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if not plan:
        return []
    names = [f"{m.app_label}.{m.name}" for m, _ in plan]
    return [Finding(
        level=INFO,
        code="preflight.I001",
        message=f"{len(names)} migration(s) will be applied.",
        fix="",
        context={"migrations": names[:50]},
    )]


def check_oauth_redirect_uris() -> list[Finding]:
    """Report the redirect_uri this deployment WILL send.

    It has to match what is registered in each provider's console — a
    third-party contract no code change can update, and one a URL-canon
    change silently invalidates.
    """
    from django.conf import settings

    try:
        from stapel_auth.conf import auth_settings
    except Exception:
        return []
    providers = [p for p, cfg in (auth_settings.OAUTH_PROVIDERS or {}).items()
                 if getattr(cfg, "client_id", "")]
    if not providers:
        return []
    base = (getattr(settings, "OAUTH_CALLBACK_BASE_URL", "") or "").rstrip("/")
    prefix = getattr(settings, "URL_PREFIX", "")
    template = getattr(auth_settings, "OAUTH_CALLBACK_PATH",
                       "/{url_prefix}api/v1/oauth/{provider}/callback")
    uris = {
        p: (base or "<host>") + template.format(url_prefix=prefix, provider=p)
        for p in sorted(providers)
    }
    return [Finding(
        level=INFO,
        code="preflight.I002",
        message="OAuth redirect_uri this deployment will send (must be registered):",
        fix="\n".join(f"  {p}: {uri}" for p, uri in uris.items()),
        context={"redirect_uris": uris},
    )]


def check_peer_internal_routes() -> list[Finding]:
    """Does the peer this service calls actually serve the path it calls?

    Owner-reported live incident, 2026-07-26: stapel-workspaces moved its API
    under `v1/`, this fleet's membership client kept asking for the pre-v1
    path, Django answered 404, and the client read that as "not a member" —
    so the workspace's own OWNER was told `Forbidden: not a member of this
    workspace`, for weeks, on a green CI. Every system check in this
    codebase validates a service's OWN urlconf; nothing had ever looked
    outward at a peer, which is precisely where the contract broke.

    Read-only: it asks about a nil UUID, so any answer at all — including
    "no such membership" — proves the route exists.
    """
    import requests
    from django.apps import apps

    from stapel_core.django.workspaces import (
        INTERNAL_API_PREFIXES,
        SERVICE_API_KEY,
        WORKSPACES_SERVICE_URL,
        _service_answered,
    )

    if not WORKSPACES_SERVICE_URL:
        return []
    # A monolith answers membership IN-PROCESS: with stapel_workspaces
    # installed, hosts import `stapel_workspaces.permissions.require_role`
    # directly and this HTTP client is never called — so probing a peer URL
    # (which defaults to a service hostname that exists only in a
    # microservice compose) reports a broken peer that nothing talks to.
    # Found on meettoday, 2026-07-26: W002 fired against
    # `http://stapel-workspaces:8000` in a monolith whose workspaces app is
    # local. A check that warns about a topology it never established is the
    # same defect it exists to catch.
    if apps.is_installed("stapel_workspaces"):
        return []
    nil = "00000000-0000-0000-0000-000000000000"
    headers = {"Accept": "application/json"}
    if SERVICE_API_KEY:
        headers["X-API-KEY"] = SERVICE_API_KEY

    tried: list[str] = []
    for prefix in INTERNAL_API_PREFIXES:
        url = f"{WORKSPACES_SERVICE_URL}{prefix}/{nil}/members/{nil}"
        tried.append(prefix)
        try:
            resp = requests.get(url, headers=headers, timeout=3)
        except Exception as exc:
            return [Finding(
                level=WARNING,
                code="preflight.W002",
                message=f"workspaces service unreachable at {WORKSPACES_SERVICE_URL}: {exc}",
                fix="If this deployment does use workspaces, it will answer "
                    "every membership question with 'not a member'.",
                context={"url": WORKSPACES_SERVICE_URL},
            )]
        if resp.status_code == 404 and not _service_answered(resp):
            continue  # wrong mount point; try the next known one
        return []  # something served it — the contract holds

    return [Finding(
        level=ERROR,
        code="preflight.E004",
        message=(
            "The workspaces internal API answers on NONE of the paths this "
            "version of stapel-core knows. Every membership check will come "
            "back 'not a member', and users — including workspace owners — "
            "will be told they lack access."
        ),
        fix=(
            "This service and stapel-workspaces disagree about where the "
            "internal API is mounted. Upgrade whichever is older (the path "
            "moved under v1/ in stapel-workspaces 0.4.2). Tried: "
            + ", ".join(tried)
        ),
        context={"service_url": WORKSPACES_SERVICE_URL, "tried": tried},
    )]


CHECKS = (
    check_system_checks,
    check_migration_adoption,
    check_bus_topology,
    check_transport_dependencies,
    check_pending_migrations,
    check_oauth_redirect_uris,
    check_peer_internal_routes,
)


class Command(BaseCommand):
    help = "Verify this deployment can take the current code (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Machine-readable output")

    def handle(self, *args, **options):
        findings: list[Finding] = []
        for check in CHECKS:
            try:
                findings.extend(check())
            except Exception as exc:  # a broken check must not mask the rest
                findings.append(Finding(
                    level=WARNING,
                    code="preflight.W000",
                    message=f"{check.__name__} could not run: {exc}",
                ))

        errors = [f for f in findings if f.level == ERROR]
        if options["json"]:
            self.stdout.write(jsonlib.dumps(
                {"ok": not errors, "findings": [asdict(f) for f in findings]},
                indent=2, ensure_ascii=False,
            ))
        else:
            for f in findings:
                self.stdout.write(f"[{f.level.upper()}] {f.code}: {f.message}")
                if f.fix:
                    self.stdout.write(f"        → {f.fix}")
            self.stdout.write(
                self.style.ERROR(f"preflight: {len(errors)} blocking issue(s)")
                if errors
                else self.style.SUCCESS("preflight: ready to deploy")
            )
        if errors:
            raise SystemExit(1)
