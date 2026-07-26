"""`manage.py stapel_preflight` — ask the deployment questions BEFORE
touching it.

Each check here exists because the corresponding failure took down the
ironmemo stand on 2026-07-25/26, and in every case the information needed
to predict it was already sitting in the settings or the database.
"""
import json

import pytest
from io import StringIO

from stapel_core.django.management.commands import stapel_preflight as pf


def run() -> dict:
    """Drive the command object directly — the umbrella app that would make
    it discoverable by name is not in this suite's INSTALLED_APPS."""
    buf = StringIO()
    try:
        pf.Command(stdout=buf).handle(json=True)
    except SystemExit:
        pass
    return json.loads(buf.getvalue())


@pytest.mark.django_db
def test_reports_ready_when_no_check_objects(monkeypatch):
    """`ok` mirrors the absence of ERROR findings, and every finding keeps
    the level/code/message/fix/context shape a release UI renders.

    (The suite as a whole cannot serve as the "healthy deployment" fixture:
    other test modules deliberately register models that trip system
    checks, and `check_system_checks` faithfully reports them.)"""
    monkeypatch.setattr(
        pf, "CHECKS",
        (lambda: [pf.Finding(level=pf.INFO, code="preflight.I999", message="fine")],),
    )
    report = run()
    assert report["ok"] is True
    assert report["findings"] == [{
        "level": "info", "code": "preflight.I999",
        "message": "fine", "fix": "", "context": {},
    }]


@pytest.mark.django_db
def test_django_check_errors_are_surfaced_as_findings(monkeypatch):
    """Deploy scripts only meet these inside the container's `migrate` —
    i.e. after the point of no return. Preflight hoists them out.

    The Error is injected rather than harvested from the ambient suite: this
    case used to rely on OTHER test modules registering check-tripping
    models, so it passed in a full run and failed when this file was run
    alone — and that is how 0.15.0 went out on a red test."""
    from django.core.checks import Error as CheckError

    monkeypatch.setattr(
        pf, "run_checks_impl",
        lambda: [CheckError("boom", hint="do the thing", id="fields.E001")],
        raising=False,
    )
    findings = pf.check_system_checks()
    assert [f.code for f in findings] == ["check.fields.E001"]
    assert findings[0].level == pf.ERROR
    assert findings[0].message == "boom"
    assert findings[0].fix == "do the thing"


@pytest.mark.django_db
def test_flags_an_unapplied_initial_migration_over_an_existing_table(monkeypatch):
    """The app-rename hazard: rows exist under another app label, so a
    plain CreateModel would abort `migrate` at container boot."""
    from django.db import migrations, models

    class FakeMigration:
        initial = True
        operations = [
            migrations.CreateModel(
                name="Ghost",
                fields=[("id", models.AutoField(primary_key=True))],
                options={"db_table": "stapel_tasks_taskrecord"},  # exists already
            )
        ]

    class FakeLoader:
        def __init__(self, *a, **kw):
            self.applied_migrations = {}
            self.disk_migrations = {("ghosts", "0001_initial"): FakeMigration()}

    monkeypatch.setattr(
        "django.db.migrations.loader.MigrationLoader", FakeLoader
    )
    findings = pf.check_migration_adoption()
    assert [f.code for f in findings] == ["preflight.E001"]
    assert "stapel_tasks_taskrecord" in findings[0].message
    assert "fake-initial" in findings[0].fix


def test_flags_an_in_process_bus_behind_a_bus_transport(settings):
    settings.STAPEL_BUS_BACKEND = "stapel_core.bus.backends.memory.MemoryBus"
    settings.STAPEL_COMM = {"ACTION_TRANSPORT": "bus"}
    findings = pf.check_bus_topology()
    assert [f.code for f in findings] == ["preflight.E002"]
    assert "KafkaBus" in findings[0].fix


def test_accepts_an_in_process_bus_when_nothing_consumes_across_processes(settings):
    settings.STAPEL_BUS_BACKEND = "stapel_core.bus.backends.memory.MemoryBus"
    settings.STAPEL_COMM = {"ACTION_TRANSPORT": "inprocess"}
    assert pf.check_bus_topology() == []


def test_flags_a_transport_whose_client_library_is_missing(settings, monkeypatch):
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "nats"}
    settings.STAPEL_BUS_BACKEND = "stapel_core.bus.backends.memory.MemoryBus"

    def missing(name):
        if name == "nats":
            raise ImportError("no module named nats")
        return object()

    monkeypatch.setattr(pf.importlib, "import_module", missing)
    findings = pf.check_transport_dependencies()
    assert [f.code for f in findings] == ["preflight.E003"]
    assert "stapel-core[nats]" in findings[0].fix


def test_transport_check_is_quiet_when_the_library_is_present(settings, monkeypatch):
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "nats"}
    settings.STAPEL_BUS_BACKEND = "stapel_core.bus.backends.memory.MemoryBus"
    monkeypatch.setattr(pf.importlib, "import_module", lambda name: object())
    assert pf.check_transport_dependencies() == []


@pytest.mark.django_db
def test_a_broken_check_degrades_to_a_warning(monkeypatch):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(pf, "CHECKS", (boom,))
    report = run()
    assert report["ok"] is True
    assert report["findings"][0]["code"] == "preflight.W000"
    assert "kaboom" in report["findings"][0]["message"]


@pytest.mark.django_db
def test_exit_code_blocks_the_deploy(monkeypatch):
    def blocker():
        return [pf.Finding(level=pf.ERROR, code="preflight.E999", message="nope")]

    monkeypatch.setattr(pf, "CHECKS", (blocker,))
    with pytest.raises(SystemExit):
        pf.Command(stdout=StringIO()).handle(json=False)


@pytest.mark.django_db
def test_reports_the_oauth_redirect_uris_a_deployment_will_send(settings, monkeypatch):
    """A URL-canon change silently invalidates a registration nobody can
    edit from code — so the value is printed, every time, before deploy."""
    class FakeCfg:
        client_id = "abc"

    class FakeAuthSettings:
        OAUTH_PROVIDERS = {"google": FakeCfg()}
        OAUTH_CALLBACK_PATH = "/{url_prefix}api/v1/oauth/{provider}/callback"

    import sys
    import types

    module = types.ModuleType("stapel_auth.conf")
    module.auth_settings = FakeAuthSettings()
    package = types.ModuleType("stapel_auth")
    package.conf = module
    monkeypatch.setitem(sys.modules, "stapel_auth", package)
    monkeypatch.setitem(sys.modules, "stapel_auth.conf", module)
    settings.OAUTH_CALLBACK_BASE_URL = "https://app.example.com"
    settings.URL_PREFIX = "auth/"

    findings = pf.check_oauth_redirect_uris()
    assert findings[0].code == "preflight.I002"
    assert (
        findings[0].context["redirect_uris"]["google"]
        == "https://app.example.com/auth/api/v1/oauth/google/callback"
    )


class TestPeerInternalRoutes:
    """Nothing in this codebase had ever checked a PEER's contract — every
    system check validates a service's own urlconf. That is exactly where it
    broke: stapel-workspaces moved its API under v1/, the membership client
    kept calling the old path, and the 404 was read as "not a member" (see
    tests/test_workspaces_membership_lookup.py)."""

    @staticmethod
    def _resp(status, *, html=False):
        class R:
            status_code = status
            headers = {
                "Content-Type": "text/html" if html else "application/json"
            }
        return R()

    def test_quiet_when_a_known_mount_point_answers(self, monkeypatch):
        import requests
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: self._resp(404)  # DRF's "no such member"
        )
        assert pf.check_peer_internal_routes() == []

    def test_errors_when_no_known_mount_point_answers(self, monkeypatch):
        import requests
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: self._resp(404, html=True)
        )
        findings = pf.check_peer_internal_routes()
        assert [f.code for f in findings] == ["preflight.E004"]
        assert "not a member" in findings[0].message
        assert "v1/" in findings[0].fix

    def test_an_unreachable_peer_warns_rather_than_blocking(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(requests, "get", boom)
        findings = pf.check_peer_internal_routes()
        assert [f.code for f in findings] == ["preflight.W002"]
        assert findings[0].level == pf.WARNING

    def test_a_monolith_is_not_probed_over_http_at_all(self, monkeypatch):
        """With workspaces installed locally, membership never leaves the process.

        meettoday, 2026-07-26: this check warned that
        `http://stapel-workspaces:8000` was unreachable in a monolith whose
        workspaces app is installed and whose hosts import
        `stapel_workspaces.permissions.require_role` directly. The peer URL
        is a compose-only default; nothing calls it. Warning about a
        topology the check never established is exactly the defect it was
        written to catch.
        """
        import requests
        from django.apps import apps

        def boom(*a, **k):  # must never be reached
            raise AssertionError("probed a peer that is installed in-process")

        monkeypatch.setattr(requests, "get", boom)
        monkeypatch.setattr(
            apps, "is_installed", lambda label: label == "stapel_workspaces"
        )
        assert pf.check_peer_internal_routes() == []
