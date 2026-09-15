"""``env_enum`` — the environment may choose among the library's own names.

THE DEFECT THIS FAMILY CLOSES

An ``import_strings`` key is implicitly env-closed, which is right about the
threat (whatever can set a variable in the pod would otherwise choose the
class on the privileged path) and wrong about the need: picking a mail
backend per environment is exactly what an env var is for. With only
``env_overridable`` — all-or-nothing — libraries documented the variable
anyway and hosts re-implemented ``os.getenv`` in their own settings modules
to make it work. One fleet's notifications service did precisely that: the
documented ``EMAIL_PROVIDER`` variable worked there and nowhere else, while
W001 truthfully reported it ignored, and reading that warning as "so mail is
going nowhere" was backwards — mail was going out for real (2026-09-16).

``env_enum`` splits the difference along the line of the actual threat.
Choosing among implementations the LIBRARY ships is a deployment decision and
belongs in the environment; naming new code to import is a trust decision and
stays in the settings module, which only the project can write.

Keys carry an ENUMCHECK_ prefix for the same reason the W001 tests do: the env
var name IS the key name, and the checks walk every AppSettings alive in the
process.
"""
import pytest
from django.core import checks as django_checks
from django.core.exceptions import ImproperlyConfigured

from stapel_core.conf import AppSettings
from stapel_core.conf_checks import (
    E003_ENV_ENUM_REJECTED,
    W001_ENV_VAR_IGNORED,
    check_env_enum_values,
    check_ignored_env_vars,
)

IMPL = "stapel_core.bus.backends.memory.MemoryBus"
NAMES = ("resend", "smtp", "mock", "unconfigured")


def _settings(**kwargs):
    return AppSettings(
        "STAPEL_ENUMCHECK",
        defaults={"ENUMCHECK_PROVIDER": "unconfigured"},
        import_strings=("ENUMCHECK_PROVIDER",),
        **kwargs,
    )


def _enum(**kwargs):
    return _settings(env_enum={"ENUMCHECK_PROVIDER": NAMES}, **kwargs)


def _findings(check, env_name):
    return [f for f in check() if env_name in f.msg]


# ── the environment now reaches the key ─────────────────────────


def test_a_short_name_from_the_environment_is_used(monkeypatch):
    """The whole point: no host has to re-implement os.getenv for this."""
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "mock")
    assert _enum()._raw("ENUMCHECK_PROVIDER") == "mock"


def test_without_the_declaration_the_same_var_is_still_ignored(monkeypatch):
    """The guard on the test above: it is the declaration doing the work."""
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "mock")
    assert _settings()._raw("ENUMCHECK_PROVIDER") == "unconfigured"


def test_an_enum_key_is_no_longer_reported_as_ignored(monkeypatch):
    """W001 must stop crying wolf once the variable genuinely works.

    Its own key name: the check walks every AppSettings alive in the process
    and dedups per (namespace, env var), so an instance another test built
    under the shared name would answer for this one.
    """
    monkeypatch.setenv("ENUMCHECK_W001_PROVIDER", "mock")
    AppSettings(
        "STAPEL_ENUMCHECK_W001",
        defaults={"ENUMCHECK_W001_PROVIDER": "unconfigured"},
        import_strings=("ENUMCHECK_W001_PROVIDER",),
        env_enum={"ENUMCHECK_W001_PROVIDER": NAMES},
    )
    assert _findings(check_ignored_env_vars, "ENUMCHECK_W001_PROVIDER") == []


def test_the_same_key_without_the_declaration_is_reported_as_ignored(monkeypatch):
    """The guard on the test above — W001 still fires when it should."""
    monkeypatch.setenv("ENUMCHECK_W001B_PROVIDER", "mock")
    AppSettings(
        "STAPEL_ENUMCHECK_W001B",
        defaults={"ENUMCHECK_W001B_PROVIDER": "unconfigured"},
        import_strings=("ENUMCHECK_W001B_PROVIDER",),
    )
    found = _findings(check_ignored_env_vars, "ENUMCHECK_W001B_PROVIDER")
    assert len(found) == 1 and found[0].id == W001_ENV_VAR_IGNORED


def test_the_settings_module_still_outranks_the_environment(monkeypatch, settings):
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "mock")
    settings.STAPEL_ENUMCHECK = {"ENUMCHECK_PROVIDER": IMPL}
    assert _enum()._raw("ENUMCHECK_PROVIDER") == IMPL


# ── but only to names the library ships ─────────────────────────


def test_a_dotted_path_from_the_environment_is_refused(monkeypatch):
    """THE test. Arbitrary import is the threat the closure exists for, and
    opening the door to short names must not open it to this."""
    monkeypatch.setenv("ENUMCHECK_PROVIDER", IMPL)
    with pytest.raises(ImproperlyConfigured) as exc:
        _enum()._raw("ENUMCHECK_PROVIDER")
    assert "dotted import path" in str(exc.value)
    assert "settings module" in str(exc.value)


def test_a_dotted_path_in_the_settings_module_is_still_accepted(settings):
    """The capability is not removed, only moved to the file that can be
    trusted with it."""
    settings.STAPEL_ENUMCHECK = {"ENUMCHECK_PROVIDER": IMPL}
    assert _enum()._raw("ENUMCHECK_PROVIDER") == IMPL


def test_an_unknown_bare_name_is_refused_and_lists_what_is_known(monkeypatch):
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "resedn")
    with pytest.raises(ImproperlyConfigured) as exc:
        _enum()._raw("ENUMCHECK_PROVIDER")
    message = str(exc.value)
    assert "resedn" in message
    for name in NAMES:
        assert name in message


def test_refusing_is_not_ignoring(monkeypatch):
    """A rejected value must not quietly resolve to the default.

    Ignoring is what the blanket closure already did, and it is what produced
    a documented variable that did nothing. Once an operator has been told the
    variable works, a value it will not take deserves an answer.
    """
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "nonsense")
    s = _enum()
    with pytest.raises(ImproperlyConfigured):
        s._raw("ENUMCHECK_PROVIDER")


# ── found at boot, not at the first passcode ────────────────────


def test_a_rejected_value_is_an_error_at_check_time(monkeypatch):
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "nonsense")
    _enum()
    found = _findings(check_env_enum_values, "ENUMCHECK_PROVIDER")
    assert len(found) == 1, found
    (error,) = found
    assert error.id == E003_ENV_ENUM_REJECTED
    assert isinstance(error, django_checks.Error)  # the process is NOT safe
    assert "nonsense" in error.msg


def test_the_check_says_the_right_thing_about_a_dotted_path(monkeypatch):
    monkeypatch.setenv("ENUMCHECK_PROVIDER", IMPL)
    _enum()
    (error,) = _findings(check_env_enum_values, "ENUMCHECK_PROVIDER")
    assert "trust decision" in error.msg


def test_an_accepted_value_produces_no_finding(monkeypatch):
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "smtp")
    _enum()
    assert _findings(check_env_enum_values, "ENUMCHECK_PROVIDER") == []


def test_an_unset_variable_produces_no_finding():
    _enum()
    assert _findings(check_env_enum_values, "ENUMCHECK_PROVIDER") == []


def test_the_check_is_registered_with_django(monkeypatch):
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "nonsense")
    _enum()
    found = [
        f for f in django_checks.run_checks()
        if getattr(f, "id", "") == E003_ENV_ENUM_REJECTED
        and "ENUMCHECK_PROVIDER" in f.msg
    ]
    assert found, "E003 is not reachable from manage.py check"


# ── the vocabulary may arrive lazily ────────────────────────────


def test_a_callable_vocabulary_is_asked_at_read_time(monkeypatch):
    """A registry is a dict in a provider module; importing it from a
    package's conf.py at declaration time would drag that module into every
    import of the package."""
    calls = []

    def vocabulary():
        calls.append(1)
        return ("alpha", "beta")

    monkeypatch.setenv("ENUMCHECK_PROVIDER", "beta")
    s = _settings(env_enum={"ENUMCHECK_PROVIDER": vocabulary})
    assert calls == [], "the vocabulary was resolved at declaration time"
    assert s._raw("ENUMCHECK_PROVIDER") == "beta"
    assert calls, "the vocabulary was never asked"


def test_a_dotted_vocabulary_is_imported_lazily(monkeypatch):
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "memory")
    s = _settings(
        env_enum={"ENUMCHECK_PROVIDER": f"{__name__}._late_vocabulary"}
    )
    assert s._raw("ENUMCHECK_PROVIDER") == "memory"


def _late_vocabulary():
    return ("memory", "kafka")


def test_a_registry_that_grows_is_seen(monkeypatch):
    """Resolved per read, so a channel registered after boot counts."""
    registry = {"alpha": object()}
    monkeypatch.setenv("ENUMCHECK_PROVIDER", "gamma")
    s = _settings(env_enum={"ENUMCHECK_PROVIDER": lambda: tuple(registry)})

    with pytest.raises(ImproperlyConfigured):
        s._raw("ENUMCHECK_PROVIDER")

    registry["gamma"] = object()
    assert s._raw("ENUMCHECK_PROVIDER") == "gamma"


# ── the declaration cannot be written ambiguously ───────────────


def test_env_enum_and_env_overridable_together_is_a_construction_error():
    with pytest.raises(ValueError) as exc:
        _settings(
            env_enum={"ENUMCHECK_PROVIDER": NAMES},
            env_overridable=("ENUMCHECK_PROVIDER",),
        )
    assert "decorative" in str(exc.value)


def test_env_enum_and_no_env_together_is_a_construction_error():
    with pytest.raises(ValueError) as exc:
        _settings(
            env_enum={"ENUMCHECK_PROVIDER": NAMES},
            no_env=("ENUMCHECK_PROVIDER",),
        )
    assert "opens the environment step" in str(exc.value)


def test_an_enum_on_an_unknown_key_is_a_construction_error():
    """A typo here would silently never apply — the exact silence this
    family was added to end."""
    with pytest.raises(ValueError) as exc:
        _settings(env_enum={"ENUMCHEK_PROVIDER": NAMES})
    assert "not in defaults" in str(exc.value)


# ── an enum key is still a plain key in every other respect ─────


def test_a_non_import_strings_key_may_also_carry_a_vocabulary(monkeypatch):
    """The family is about the VALUE, not about import_strings membership:
    a plain scalar key whose values are enumerable gets the same guard."""
    monkeypatch.setenv("ENUMCHECK_MODE", "strict")
    s = AppSettings(
        "STAPEL_ENUMCHECK2",
        defaults={"ENUMCHECK_MODE": "lax"},
        env_enum={"ENUMCHECK_MODE": ("lax", "strict")},
    )
    assert s._raw("ENUMCHECK_MODE") == "strict"

    monkeypatch.setenv("ENUMCHECK_MODE", "strictt")
    s.reload()
    with pytest.raises(ImproperlyConfigured):
        s._raw("ENUMCHECK_MODE")
