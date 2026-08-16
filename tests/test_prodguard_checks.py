"""``guard_secret`` stops being a call somebody has to remember.

The guards have existed since 0.8.1 and were imported by exactly one thing:
the prod settings tier ``stapel-tools`` GENERATES. A project not scaffolded by
``stapel-create-project`` — or scaffolded before the template grew the call —
gets nothing, and nothing anywhere reports the absence: no check reads them,
and ``manage.py check`` cannot surface an ``ImproperlyConfigured`` a settings
module never raised. That is how a six-character SECRET_KEY boots production.

These pin the closure: the same two functions, run from the check registry
every project already inherits, on the boot-gate roster so they reach gunicorn
— which is where the settings-module call would have run and didn't.
"""
import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from stapel_core.django.prodguard import (
    AUTO,
    E001_WEAK_SECRET,
    E002_WEAK_DB_PASSWORD,
    MIN_SECRET_LENGTH,
    OFF,
    W001_PRODGUARD_OFF,
    check_production_secrets,
    guard_secret,
    prodguard_mode,
)

GOOD_SECRET = "z" * (MIN_SECRET_LENGTH + 14)


def postgres(password):
    return {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "app",
            "PASSWORD": password,
        }
    }


def ids_of(findings):
    return [f.id for f in findings]


#: The suite already runs on sqlite, so only the postgres cases override
#: DATABASES (and only they carry the override warning).
ENFORCING = dict(STAPEL_PRODGUARD="enforce")

pytestmark = pytest.mark.filterwarnings(
    "ignore:Overriding setting DATABASES:UserWarning"
)


# ---------------------------------------------------------------------------
# The gap, closed
# ---------------------------------------------------------------------------


def test_a_six_character_secret_is_refused_without_anyone_calling_the_guard():
    """The whole point. No settings module calls guard_secret here — the check
    registry does. The mutant: drop `register_checks`/this function and a
    project that never wrote the call is back to booting on `abc123`."""
    with override_settings(SECRET_KEY="abc123", **ENFORCING):
        findings = check_production_secrets()
    assert ids_of(findings) == [E001_WEAK_SECRET]
    assert findings[0].level >= checks.ERROR


def test_a_placeholder_secret_is_refused():
    with override_settings(SECRET_KEY="change_me_to_a_long_random_string", **ENFORCING):
        assert ids_of(check_production_secrets()) == [E001_WEAK_SECRET]


def test_a_real_secret_passes():
    with override_settings(SECRET_KEY=GOOD_SECRET, **ENFORCING):
        assert check_production_secrets() == []


def test_the_finding_never_carries_the_value():
    """A check that prints the secret it is complaining about is a worse
    problem than the one it found."""
    secret = "abc123"
    with override_settings(SECRET_KEY=secret, **ENFORCING):
        findings = check_production_secrets()
    blob = " ".join(f"{f.msg} {f.hint}" for f in findings)
    assert secret not in blob
    assert "6 characters long" in blob


def test_the_check_runs_the_guard_rather_than_reimplementing_it(monkeypatch):
    """One rule, in one place. A second copy of the placeholder list is a
    second thing to forget to update."""
    seen = []

    def spy(name, value, **kwargs):
        seen.append(name)
        raise ImproperlyConfigured("spied")

    monkeypatch.setattr("stapel_core.django.prodguard.guard_secret", spy)
    with override_settings(SECRET_KEY=GOOD_SECRET, **ENFORCING):
        findings = check_production_secrets()
    assert seen == ["SECRET_KEY"]
    assert findings[0].msg == "spied"


def test_extra_secrets_can_be_named_without_a_core_release():
    with override_settings(
        SECRET_KEY=GOOD_SECRET,
        JWT_SECRET_KEY="short",
        STAPEL_PRODGUARD_SECRETS=["JWT_SECRET_KEY"],
        **ENFORCING,
    ):
        findings = check_production_secrets()
    assert ids_of(findings) == [E001_WEAK_SECRET]
    assert "JWT_SECRET_KEY" in findings[0].msg


# ---------------------------------------------------------------------------
# The database password
# ---------------------------------------------------------------------------


def test_the_shipped_database_password_is_refused():
    with override_settings(
        SECRET_KEY=GOOD_SECRET, STAPEL_PRODGUARD="enforce", DATABASES=postgres("stapel")
    ):
        assert ids_of(check_production_secrets()) == [E002_WEAK_DB_PASSWORD]


def test_sqlite_is_not_asked_for_a_password():
    """The mutant: call guard_db_password unconditionally — an empty password
    is a placeholder to that function, and every sqlite deployment (including
    every library's own test suite) turns red over a field it has no use for."""
    with override_settings(SECRET_KEY=GOOD_SECRET, **ENFORCING):
        assert check_production_secrets() == []


def test_a_real_database_password_passes():
    with override_settings(
        SECRET_KEY=GOOD_SECRET,
        STAPEL_PRODGUARD="enforce",
        DATABASES=postgres("F9x2-generated-value"),
    ):
        assert check_production_secrets() == []


# ---------------------------------------------------------------------------
# When it applies
# ---------------------------------------------------------------------------


def test_auto_is_off_under_debug():
    with override_settings(STAPEL_PRODGUARD=AUTO, DEBUG=True):
        assert prodguard_mode() == OFF


def test_auto_is_off_under_a_test_runner():
    """A package's own suite configures a fake SECRET_KEY and no DEBUG, which
    is production-shaped to every signal Django exposes. Enforcing there turns
    every library's CI red over a value that is correct for it — and a check
    that floods is a check that gets silenced wholesale on day one."""
    with override_settings(STAPEL_PRODGUARD=AUTO, DEBUG=False):
        assert prodguard_mode() == OFF  # pytest is in sys.modules


def test_auto_enforces_in_a_real_deployment(monkeypatch):
    monkeypatch.setattr(
        "stapel_core.django.prodguard._under_test_runner", lambda: False
    )
    with override_settings(STAPEL_PRODGUARD=AUTO, DEBUG=False):
        assert prodguard_mode() == "enforce"


def test_an_unreadable_switch_means_auto_not_off(monkeypatch):
    """A typo in the switch is exactly the moment somebody ships an ungated
    fleet, so it must not be the thing that opens the gate."""
    monkeypatch.setattr(
        "stapel_core.django.prodguard._under_test_runner", lambda: False
    )
    with override_settings(STAPEL_PRODGUARD="maybe", DEBUG=False):
        assert prodguard_mode() == "enforce"


def test_switching_the_guard_off_reports_itself():
    with override_settings(STAPEL_PRODGUARD="off", SECRET_KEY="abc123"):
        findings = check_production_secrets()
    assert ids_of(findings) == [W001_PRODGUARD_OFF]
    assert findings[0].level < checks.ERROR


def test_being_off_by_auto_detection_reports_nothing():
    """A W on every library test run is the flood, not the signal."""
    with override_settings(STAPEL_PRODGUARD=AUTO, DEBUG=True, SECRET_KEY="abc123"):
        assert check_production_secrets() == []


# ---------------------------------------------------------------------------
# Reach
# ---------------------------------------------------------------------------


def test_the_guard_is_on_the_boot_gate_roster():
    """Under gunicorn Django runs no checks at all — the reason the E-gate
    wave needed the boot middleware, and the reason this tag rides it."""
    from stapel_core.django.boot import BOOT_GATE_TAGS

    assert "stapel_prodguard" in BOOT_GATE_TAGS


def test_the_findings_cannot_be_muted_by_a_blanket_line():
    with override_settings(SECRET_KEY="abc123", **ENFORCING):
        finding = check_production_secrets()[0]
    with override_settings(SILENCED_SYSTEM_CHECKS=[E001_WEAK_SECRET]):
        assert finding.is_silenced() is False


def test_a_per_check_waiver_with_a_reason_still_works():
    """A project with a genuine reason must have a route — just not a blanket
    one. There is no such reason for this check, and the mechanism does not
    get to decide that per check."""
    from stapel_core.django.check_guard import WAIVERS_SETTING

    with override_settings(SECRET_KEY="abc123", **ENFORCING):
        finding = check_production_secrets()[0]
    with override_settings(**{WAIVERS_SETTING: {E001_WEAK_SECRET: "documented"}}):
        assert finding.is_silenced() is True


def test_the_original_functions_are_untouched():
    """The check is a new caller, not a rewrite: a settings tier that already
    calls guard_secret keeps working exactly as it did."""
    with pytest.raises(ImproperlyConfigured):
        guard_secret("SECRET_KEY", "abc123")
    guard_secret("SECRET_KEY", GOOD_SECRET)


class TestDummyBackendHasNoPasswordToBeWeak:
    """E002 must not fire on Django's `dummy` database backend.

    ``_default_db_wants_a_password`` already states the rule it means to
    apply — "Only engines that authenticate with one. SQLite has no
    password." — and excludes sqlite for exactly that reason. The dummy
    backend authenticates with even less: it cannot open a connection at
    all, so there is no password for an attacker to guess and nothing the
    check can meaningfully assert.

    This is not a hole in the guard. It is the difference between "this
    deployment ships a public default password" and "this configuration has
    no database", which is the same distinction the whole prodguard tier
    rests on. Generated projects' boot-smoke tier swaps DATABASES for the
    dummy backend on purpose (proving app loading needs no database), and
    without this it goes red on a credential that does not exist — pushing
    every generated project to carry a workaround for a predicate that
    simply forgot a backend.
    """

    def test_dummy_backend_is_not_asked_for_a_password(self, settings):
        from stapel_core.django.prodguard import _default_db_wants_a_password

        settings.DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
        assert _default_db_wants_a_password() is False

    def test_postgres_is_still_asked(self, settings):
        from stapel_core.django.prodguard import _default_db_wants_a_password

        settings.DATABASES = {
            "default": {"ENGINE": "django.db.backends.postgresql", "PASSWORD": "x"}
        }
        assert _default_db_wants_a_password() is True
