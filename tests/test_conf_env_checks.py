"""The ignored-env-var system check (stapel_core.conf_checks).

Since an ``import_strings`` key is implicitly ``no_env``, a deployment that
selected an implementation with a bare env var runs different code and hears
nothing about it. These tests pin the alarm: it fires exactly when a variable
is set AND dropped, it names that variable, and Django actually runs it.

Keys here carry a CONFCHECK_ prefix on purpose: the env var name IS the key
name, and the check walks every AppSettings alive in the process, so a
generic key (PROVIDER) would collide with namespaces other tests built.
"""
import os
import subprocess
import sys
import textwrap

from django.core import checks as django_checks

from stapel_core.conf import AppSettings, registered_settings
from stapel_core.conf_checks import W001_ENV_VAR_IGNORED, check_ignored_env_vars

IMPL = "stapel_core.bus.backends.memory.MemoryBus"


def _findings(env_name):
    """Check findings that mention *env_name* — the rest of the process's
    namespaces are none of this test's business."""
    return [w for w in check_ignored_env_vars() if env_name in w.msg]


def test_a_set_env_var_for_an_import_strings_key_warns(monkeypatch):
    """The silent case, made loud: the var is set and simply not read."""
    monkeypatch.setenv("CONFCHECK_PROVIDER", IMPL)
    s = AppSettings(
        "STAPEL_CONFCHECK",
        defaults={"CONFCHECK_PROVIDER": IMPL},
        import_strings=("CONFCHECK_PROVIDER",),
    )
    found = _findings("CONFCHECK_PROVIDER")
    assert len(found) == 1, found
    (warning,) = found
    assert warning.id == W001_ENV_VAR_IGNORED
    assert isinstance(warning, django_checks.Warning)  # never blocks a deploy
    assert s.namespace in warning.msg
    assert "CONFCHECK_PROVIDER" in warning.msg  # the variable, by name
    assert "env_overridable" in warning.hint  # remedy 2
    assert s.namespace in warning.hint  # remedy 1: the settings dict
    assert IMPL not in warning.msg + (warning.hint or "")  # names, never values


def test_env_overridable_key_is_silent(monkeypatch):
    """The var is read, so there is nothing to report."""
    monkeypatch.setenv("CONFCHECK_OPT_OUT", IMPL)
    AppSettings(
        "STAPEL_CONFCHECK_OPT_OUT",
        defaults={"CONFCHECK_OPT_OUT": ""},
        import_strings=("CONFCHECK_OPT_OUT",),
        env_overridable=("CONFCHECK_OPT_OUT",),
    )
    assert _findings("CONFCHECK_OPT_OUT") == []


def test_env_var_for_a_plain_key_is_silent(monkeypatch):
    """A plain key still resolves from the environment — nothing changed."""
    monkeypatch.setenv("CONFCHECK_PLAIN", "value")
    AppSettings(
        "STAPEL_CONFCHECK_PLAIN",
        defaults={"CONFCHECK_PLAIN": "default"},
    )
    assert _findings("CONFCHECK_PLAIN") == []


def test_an_import_strings_key_without_an_env_var_is_silent():
    """No variable set, no contradicted intent, no noise."""
    AppSettings(
        "STAPEL_CONFCHECK_QUIET",
        defaults={"CONFCHECK_QUIET": IMPL},
        import_strings=("CONFCHECK_QUIET",),
    )
    assert _findings("CONFCHECK_QUIET") == []


def test_discovery_sees_every_namespace_that_was_imported():
    """Non-vacuity: the check walks the fleet, not one hard-coded module.

    A check that could only ever see ``stapel_core.access`` would report
    nothing for the sibling repos this rule actually broke.
    """
    import stapel_core.access.conf  # noqa: F401
    import stapel_core.media.conf  # noqa: F401
    import stapel_core.secrets.conf  # noqa: F401

    namespaces = {s.namespace for s in registered_settings()}
    assert {"STAPEL_ACCESS", "STAPEL_MEDIA", "STAPEL_SECRETS"} <= namespaces


def test_the_watermark_key_core_actually_changed_is_covered(monkeypatch):
    """The one core key whose behaviour 7a96a23 flipped is the one an
    operator is most likely to have set. Prove the live namespace — not a
    test-local AppSettings — reaches the check."""
    import stapel_core.media.conf  # noqa: F401

    monkeypatch.setenv("WATERMARK", "example.watermark.stamp")
    found = _findings("WATERMARK")
    assert [w.id for w in found] == [W001_ENV_VAR_IGNORED]
    assert "STAPEL_MEDIA" in found[0].msg


def test_the_check_is_registered_not_merely_importable(tmp_path):
    """The recurring defect is a mechanism nobody wires up.

    Asserting the registry *in this process* would prove nothing: the import
    at the top of this file registers the check by itself, so the assertion
    passes even with the line in ``CommonDjangoConfig.ready()`` deleted. The
    proof has to be a process that never names ``conf_checks`` — a service
    that installs the app config and runs the checks, i.e. ``manage.py
    check``, which is the whole promise being made to the operator.
    """
    (tmp_path / "projsettings.py").write_text(PROJECT_SETTINGS, encoding="utf-8")
    child_env = dict(os.environ)
    child_env["DJANGO_SETTINGS_MODULE"] = "projsettings"
    # tmp_path only: the repo root holds a ``django/`` package directory that
    # would shadow Django itself in the child.
    child_env["PYTHONPATH"] = str(tmp_path)
    child_env["WATERMARK"] = "example.watermark.stamp"
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import django
            django.setup()
            import stapel_core.media.conf  # a service that uses media, no more
            from django.core.checks import run_checks
            ids = [w.id for w in run_checks()]
            assert "stapel_core.conf.W001" in ids, ids
            print("OK")
        """)],
        capture_output=True, text=True, cwd=str(tmp_path), env=child_env,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


PROJECT_SETTINGS = """
SECRET_KEY = "test-only"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
]
AUTH_USER_MODEL = "users.User"
ROOT_URLCONF = "projsettings"
urlpatterns = []
"""
