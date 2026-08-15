"""The E-gates have to reach the place they were written for: production.

Django runs system checks for management commands and ``runserver``, and none
at all for ``gunicorn config.wsgi:application`` — which is exactly how every
generated Stapel project boots. The whole 0.24.0 gate wave therefore guarded
laptops and CI and, quite possibly, guarded production not at all.

These tests build a real ``WSGIHandler`` (the thing gunicorn builds) rather
than instantiating the middleware by hand, because the claim being pinned is
about Django's boot path, not about a class.
"""
import logging

import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

# The core's checks register on import of their modules; this test suite does
# not install the ``stapel_core.django`` AppConfig whose ready() normally does
# that, so the CORS gate has to be pulled in explicitly for the registry to
# have anything to run.
from stapel_core.django import cors_checks as _cors_checks  # noqa: F401
from stapel_core.django.boot import (
    BOOT_GATE_TAGS,
    MIDDLEWARE_PATH,
    W001_BOOT_GATES_NOT_ENFORCED,
    W002_BOOT_GATE_MIDDLEWARE_MISSING,
    BootGateMiddleware,
    boot_gate_mode,
    check_boot_gate_middleware_installed,
    check_boot_gates_enforced,
    format_findings,
    run_boot_gates,
)

# A configuration the 0.24.0 CORS gate refuses: allow every origin AND send
# credentials. django-cors-headers then reflects the caller's Origin.
BROKEN_CORS = {
    "CORS_ALLOW_ALL_ORIGINS": True,
    "CORS_ALLOW_CREDENTIALS": True,
}
CLEAN_CORS = {
    "CORS_ALLOW_ALL_ORIGINS": False,
    "CORS_ALLOW_CREDENTIALS": True,
}
WITH_GATE = [MIDDLEWARE_PATH, "django.middleware.common.CommonMiddleware"]


def build_wsgi_handler():
    """Exactly what ``get_wsgi_application()`` constructs under gunicorn.

    ``WSGIHandler.__init__`` calls ``load_middleware()``, which instantiates
    every middleware. That call is the boot gate's whole opportunity.
    """
    from django.core.handlers.wsgi import WSGIHandler

    return WSGIHandler()


# ---------------------------------------------------------------------------
# The refusal itself.
# ---------------------------------------------------------------------------

@override_settings(MIDDLEWARE=WITH_GATE, **BROKEN_CORS)
def test_wsgi_boot_refuses_a_configuration_the_gates_reject():
    with pytest.raises(ImproperlyConfigured) as excinfo:
        build_wsgi_handler()
    assert "stapel_core.cors.E001" in str(excinfo.value)


@override_settings(MIDDLEWARE=WITH_GATE, **CLEAN_CORS)
def test_clean_configuration_boots_and_the_gate_removes_itself():
    """MiddlewareNotUsed: the gate runs once per worker, never per request."""
    handler = build_wsgi_handler()
    chain = repr(handler._middleware_chain) + repr(
        getattr(handler, "_view_middleware", [])
    )
    assert "BootGateMiddleware" not in chain


@override_settings(MIDDLEWARE=WITH_GATE, **BROKEN_CORS)
def test_the_refusal_names_every_finding_verbatim():
    """One redeploy per discovered error is how a gate loses its audience."""
    findings = run_boot_gates()
    assert findings, "the broken config must produce findings to format"
    text = format_findings(findings)
    for finding in findings:
        assert finding.msg in text
        assert finding.id in text
        if finding.hint:
            assert finding.hint in text


@override_settings(MIDDLEWARE=WITH_GATE, **BROKEN_CORS)
def test_warn_mode_boots_and_logs_the_same_causes(caplog):
    with override_settings(STAPEL_BOOT_GATES="warn"):
        with caplog.at_level(logging.ERROR, logger="stapel_core.django.boot"):
            build_wsgi_handler()
    assert "stapel_core.cors.E001" in caplog.text


@override_settings(MIDDLEWARE=WITH_GATE, STAPEL_BOOT_GATES="off", **BROKEN_CORS)
def test_off_mode_does_not_even_run_the_checks(monkeypatch):
    # "off" must not consult anything at all.
    monkeypatch.setattr(
        "stapel_core.django.boot.run_boot_gates",
        lambda: (_ for _ in ()).throw(AssertionError("off must not run checks")),
    )
    build_wsgi_handler()


@override_settings(MIDDLEWARE=WITH_GATE, **BROKEN_CORS)
def test_a_typo_in_the_switch_still_enforces():
    """A misspelled switch must not be a silently open gate."""
    with override_settings(STAPEL_BOOT_GATES="enfroce"):
        assert boot_gate_mode() == "enforce"
        with pytest.raises(ImproperlyConfigured):
            build_wsgi_handler()


@override_settings(MIDDLEWARE=WITH_GATE, **CLEAN_CORS)
def test_warnings_alone_never_refuse_a_worker():
    """Only ERROR-or-worse refuses.

    ``stapel_conf`` is in the roster and is W-only; a boot gate that refused
    on warnings would make every stray env var a production outage.
    """
    def noisy(app_configs=None, **kwargs):
        return [checks.Warning("cosmetic", id="test.W001")]

    checks.register(noisy, "stapel_conf")
    try:
        assert any(f.id == "test.W001" for f in checks.run_checks(tags=["stapel_conf"]))
        assert run_boot_gates() == []
        build_wsgi_handler()
    finally:
        checks.registry.registry.registered_checks.discard(noisy)


# ---------------------------------------------------------------------------
# The roster. A DB-touching check sneaking in is the regression to fear.
# ---------------------------------------------------------------------------

def test_boot_gate_tag_roster_is_pinned():
    """Change this list only on purpose — each tag is a new way to refuse.

    A tag added here is a new way production can decline to start, and a
    DB-touching one turns "the database is 3 seconds behind" into a fleet-wide
    boot failure. The roster is settings-only and DB-free by construction.
    """
    assert BOOT_GATE_TAGS == (
        "stapel_auth_backends",
        "stapel_cors",
        "stapel_conf",
        "stapel_comm",
        "stapel_bus",
        "stapel_captcha",
        "stapel_check_guard",
        "stapel_prodguard",
    )


def test_url_resolving_mandate_tag_is_not_in_the_roster():
    """``stapel_mandate`` walks the URL surface — the re-entrancy trap.

    Named individually, like the DB-touching tags below, so a future "the
    mandate gate is security-critical, put it on the roster too" has to argue
    with the reason rather than with an omission.
    """
    assert "stapel_mandate" not in BOOT_GATE_TAGS


def test_cwd_dependent_and_environ_only_tags_are_not_in_the_roster():
    """``stapel_config`` came off the roster; it must not drift back on.

    It reads required keys from ``os.environ`` only (so a deployment whose
    secret arrives as ``DJANGO_SECRET_KEY`` is refused despite a valid
    ``settings.SECRET_KEY``) and finds its manifest by walking up from
    ``Path.cwd()`` (so the verdict depends on the launch directory). Neither
    property belongs in something that can refuse a worker. The check itself
    stays registered for ``manage.py check``.
    """
    from django.core import checks as django_checks

    assert "stapel_config" not in BOOT_GATE_TAGS
    registered = {
        tag
        for check in django_checks.registry.registry.get_checks()
        for tag in getattr(check, "tags", ())
    }
    assert "stapel_config" in registered


def test_boot_gates_run_no_tag_outside_the_allowlist(monkeypatch):
    seen = {}

    def spy(*args, **kwargs):
        seen["tags"] = kwargs.get("tags")
        return []

    monkeypatch.setattr("django.core.checks.run_checks", spy)
    run_boot_gates()
    assert seen["tags"] == list(BOOT_GATE_TAGS)


def test_db_touching_tags_are_not_in_the_roster():
    """Named individually so a future 'just add mounts too' has to argue."""
    for tag in ("stapel_mounts", "stapel_nav", "stapel_admin", "stapel_access",
                "stapel_adoption", "stapel_cdn"):
        assert tag not in BOOT_GATE_TAGS


@override_settings(MIDDLEWARE=WITH_GATE, **BROKEN_CORS)
def test_manage_py_check_still_reports_instead_of_crashing():
    """The rejected ``AppConfig.ready()`` design would break this.

    ``manage.py check`` on a violating config must PRINT the diagnosis. A gate
    that raises during setup takes down the only tool that could explain it.
    """
    findings = checks.run_checks()
    assert any(f.id == "stapel_core.cors.E001" for f in findings)


# ---------------------------------------------------------------------------
# The W-checks that keep every open state stated.
# ---------------------------------------------------------------------------

def test_enforce_is_the_default_and_is_silent():
    assert boot_gate_mode() == "enforce"
    assert check_boot_gates_enforced() == []


@pytest.mark.parametrize("mode", ["warn", "off"])
def test_a_downgraded_gate_reports_itself(mode):
    with override_settings(STAPEL_BOOT_GATES=mode):
        warnings = check_boot_gates_enforced()
    assert [w.id for w in warnings] == [W001_BOOT_GATES_NOT_ENFORCED]
    assert mode in warnings[0].msg


@override_settings(MIDDLEWARE=WITH_GATE)
def test_middleware_present_is_silent():
    assert check_boot_gate_middleware_installed() == []


@override_settings(MIDDLEWARE=["django.middleware.common.CommonMiddleware"])
def test_hand_rolled_middleware_without_the_gate_is_reported():
    """The only way a non-conforming project learns its gates never run."""
    warnings = check_boot_gate_middleware_installed()
    assert [w.id for w in warnings] == [W002_BOOT_GATE_MIDDLEWARE_MISSING]
    assert MIDDLEWARE_PATH in warnings[0].hint


def test_common_middleware_carries_the_gate_first():
    """Every conforming project is covered by construction, on the next bump."""
    from stapel_core.django.settings import COMMON_MIDDLEWARE

    assert COMMON_MIDDLEWARE[0] == MIDDLEWARE_PATH


def test_the_middleware_never_serves_a_request():
    """It is a boot gate, not a request hook; __call__ must be unreachable."""
    with override_settings(MIDDLEWARE=WITH_GATE, **CLEAN_CORS):
        from django.core.exceptions import MiddlewareNotUsed

        with pytest.raises(MiddlewareNotUsed):
            BootGateMiddleware(lambda request: None)
