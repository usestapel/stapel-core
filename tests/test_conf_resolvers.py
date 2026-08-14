"""W001's real scope, and the resolver family that shares its policy.

Two halves of one verdict.

**Scope.** ``ignored_env_vars()`` used to iterate ``import_strings`` alone,
while ``_env_allowed()`` already knew the full truth. So a set-but-ignored
env var on a ``no_env`` key produced no warning at all — reproducing exactly
the silence W001 was built to end. ``no_env`` was invented for the SAME
threat model (generic names that could silently change a trust decision), so
there was never a reason for the walk to ask a narrower question than the
gate answers.

**``resolvers=``.** A value that is legally "registry short name OR dotted
path" cannot go through the base class's eager ``import_string``. Packages
worked around it by subclassing ``__getattr__`` or by keeping the key out of
``import_strings`` with a comment — the second of which silently drops the
key out of W001's scope, which is the defect this file also pins shut. A
free-floating ``resolve=`` was rejected: only the string→object step is
delegated, and the key stays a member of the import_strings family for every
policy purpose.

Keys carry a RESOLV_/SCOPE_ prefix for the reason the sibling file documents:
the env var name IS the key name, and the check walks every AppSettings alive
in the process.
"""
import pytest
from django.core.exceptions import ImproperlyConfigured

from stapel_core.conf import AppSettings
from stapel_core.conf_checks import W001_ENV_VAR_IGNORED, check_ignored_env_vars

IMPL = "stapel_core.bus.backends.memory.MemoryBus"


def findings(env_name):
    return [w for w in check_ignored_env_vars() if env_name in w.msg]


class Registry:
    """Stands in for a real channel registry: short names AND dotted paths."""

    SHORT = {"memory": IMPL}

    @classmethod
    def resolve(cls, value):
        from django.utils.module_loading import import_string

        if value in cls.SHORT:
            return import_string(cls.SHORT[value])
        if "." not in value:
            raise ImproperlyConfigured(
                f"unknown provider {value!r}; known short names: "
                f"{sorted(cls.SHORT)}"
            )
        return import_string(value)


# ---------------------------------------------------------------------------
# Scope: every key the instance closes, not just import_strings.
# ---------------------------------------------------------------------------

def test_a_no_env_key_with_a_same_named_var_is_now_reported(monkeypatch):
    """The silence the narrow scope left behind.

    "The operator believes X is configured and it is not" is the whole point
    of W001, and a no_env key hits it exactly as hard as an import_strings
    one.
    """
    monkeypatch.setenv("SCOPE_TRUSTED_HEADER", "X-Whatever")
    AppSettings(
        "STAPEL_SCOPE_POLICY",
        defaults={"SCOPE_TRUSTED_HEADER": ""},
        no_env=("SCOPE_TRUSTED_HEADER",),
    )
    found = findings("SCOPE_TRUSTED_HEADER")
    assert [w.id for w in found] == [W001_ENV_VAR_IGNORED]


def test_a_no_env_key_gets_the_POLICY_wording_not_the_class_wording(monkeypatch):
    """A gate must not over-claim its cause.

    Telling an operator that a policy toggle "names the class the process
    loads" sends them hunting for a dotted path that was never there.
    """
    monkeypatch.setenv("SCOPE_ALLOW_SIGNUP", "1")
    AppSettings(
        "STAPEL_SCOPE_WORDING",
        defaults={"SCOPE_ALLOW_SIGNUP": True},
        no_env=("SCOPE_ALLOW_SIGNUP",),
    )
    (warning,) = findings("SCOPE_ALLOW_SIGNUP")
    assert "no_env" in warning.msg
    assert "names the class the process loads" not in warning.msg
    assert "no_env" in warning.hint
    assert "env_overridable" not in warning.hint  # no_env ∩ env_overridable is illegal


def test_an_import_strings_key_keeps_the_CLASS_wording(monkeypatch):
    monkeypatch.setenv("SCOPE_CLASS_PROVIDER", IMPL)
    AppSettings(
        "STAPEL_SCOPE_CLASS",
        defaults={"SCOPE_CLASS_PROVIDER": IMPL},
        import_strings=("SCOPE_CLASS_PROVIDER",),
    )
    (warning,) = findings("SCOPE_CLASS_PROVIDER")
    assert "names the class the process loads" in warning.msg
    assert "env_overridable" in warning.hint


def test_an_env_overridable_key_is_still_silent(monkeypatch):
    """It is read, so there is nothing to report — widening must not break this."""
    monkeypatch.setenv("SCOPE_OPEN_PROVIDER", IMPL)
    AppSettings(
        "STAPEL_SCOPE_OPEN",
        defaults={"SCOPE_OPEN_PROVIDER": ""},
        import_strings=("SCOPE_OPEN_PROVIDER",),
        env_overridable=("SCOPE_OPEN_PROVIDER",),
    )
    assert findings("SCOPE_OPEN_PROVIDER") == []


def test_an_ordinary_value_key_is_never_reported(monkeypatch):
    """Widening is to every CLOSED key, not to every key."""
    monkeypatch.setenv("SCOPE_PAGE_SIZE", "50")
    AppSettings("STAPEL_SCOPE_PLAIN", defaults={"SCOPE_PAGE_SIZE": 20})
    assert findings("SCOPE_PAGE_SIZE") == []


def test_media_bare_BACKEND_is_reported_but_the_alias_is_not(monkeypatch):
    """The media contract, pinned in both directions.

    ``BACKEND`` is no_env, so a bare ``BACKEND`` env var IS dropped and must
    now say so. ``STAPEL_MEDIA_BACKEND`` is honored through FLAT_ALIASES,
    deliberately outside ``env_var_names()`` — the check must never claim a
    variable was dropped when it was read.
    """
    from stapel_core.media.conf import media_settings

    monkeypatch.setenv("BACKEND", "some.other.Backend")
    monkeypatch.setenv("STAPEL_MEDIA_BACKEND", "some.other.Backend")
    reported = dict(
        (key, name) for key, name, _family in media_settings.ignored_env_vars()
    )
    assert reported.get("BACKEND") == "BACKEND"
    assert "STAPEL_MEDIA_BACKEND" not in reported.values()


def test_env_closed_keys_matches_the_gate_exactly():
    """The walk and the gate must be the same question, by construction."""
    s = AppSettings(
        "STAPEL_SCOPE_AGREE",
        defaults={"A": 1, "B": 2, "C": 3, "D": 4},
        import_strings=("B", "C"),
        no_env=("A",),
        env_overridable=("C",),
    )
    assert s.env_closed_keys() == ["A", "B"]
    for key in ("A", "B", "C", "D"):
        assert s._env_allowed(key) is (key not in s.env_closed_keys())


# ---------------------------------------------------------------------------
# resolvers=: the import_strings family with a custom string→object step.
# ---------------------------------------------------------------------------

def test_a_resolver_key_resolves_a_short_name():
    s = AppSettings(
        "STAPEL_RESOLV_SHORT",
        defaults={"RESOLV_A": "memory"},
        resolvers={"RESOLV_A": Registry.resolve},
    )
    from stapel_core.bus.backends.memory import MemoryBus

    assert s.RESOLV_A is MemoryBus


def test_a_resolver_key_resolves_a_dotted_path():
    """The reason the key could not simply be import_strings AND no_env."""
    s = AppSettings(
        "STAPEL_RESOLV_DOTTED",
        defaults={"RESOLV_B": IMPL},
        resolvers={"RESOLV_B": Registry.resolve},
    )
    from stapel_core.bus.backends.memory import MemoryBus

    assert s.RESOLV_B is MemoryBus


def test_a_resolver_given_as_a_dotted_path_is_imported_lazily():
    """conf.py must never import a channel module at declaration time."""
    s = AppSettings(
        "STAPEL_RESOLV_LAZY",
        defaults={"RESOLV_C": IMPL},
        resolvers={"RESOLV_C": "django.utils.module_loading.import_string"},
    )
    assert s.resolvers["RESOLV_C"] == "django.utils.module_loading.import_string"
    from stapel_core.bus.backends.memory import MemoryBus

    assert s.RESOLV_C is MemoryBus
    assert callable(s.resolvers["RESOLV_C"])  # imported once, cached in place


def test_resolver_errors_pass_through_with_their_own_type_and_message():
    """A registry's ImproperlyConfigured must survive.

    Degrading it to a bare ImportError throws away the only useful half of
    the message — the short names the registry does know.
    """
    s = AppSettings(
        "STAPEL_RESOLV_ERR",
        defaults={"RESOLV_D": "nonsense"},
        resolvers={"RESOLV_D": Registry.resolve},
    )
    with pytest.raises(ImproperlyConfigured) as excinfo:
        s.RESOLV_D
    assert "memory" in str(excinfo.value)


def test_a_resolver_key_is_env_closed_and_W001_visible(monkeypatch):
    monkeypatch.setenv("RESOLV_PROVIDER", IMPL)
    s = AppSettings(
        "STAPEL_RESOLV_CLOSED",
        defaults={"RESOLV_PROVIDER": "memory"},
        resolvers={"RESOLV_PROVIDER": Registry.resolve},
    )
    (warning,) = findings("RESOLV_PROVIDER")
    assert warning.id == W001_ENV_VAR_IGNORED
    # The class wording: a resolver key names an implementation, like any
    # other member of the family.
    assert "names the class the process loads" in warning.msg
    from stapel_core.bus.backends.memory import MemoryBus

    assert s.RESOLV_PROVIDER is MemoryBus  # the env var really is ignored


def test_env_overridable_reopens_a_resolver_key(monkeypatch):
    monkeypatch.setenv("RESOLV_OPEN", IMPL)
    s = AppSettings(
        "STAPEL_RESOLV_OPEN",
        defaults={"RESOLV_OPEN": "memory"},
        resolvers={"RESOLV_OPEN": Registry.resolve},
        env_overridable=("RESOLV_OPEN",),
    )
    assert findings("RESOLV_OPEN") == []
    from stapel_core.bus.backends.memory import MemoryBus

    assert s.RESOLV_OPEN is MemoryBus  # read from the env, through the resolver


def test_a_non_string_value_skips_the_resolver_entirely():
    """A settings module that already put the object there is done."""
    from stapel_core.bus.backends.memory import MemoryBus

    s = AppSettings(
        "STAPEL_RESOLV_OBJ",
        defaults={"RESOLV_E": MemoryBus},
        resolvers={"RESOLV_E": lambda v: pytest.fail("must not be called")},
    )
    assert s.RESOLV_E is MemoryBus


def test_declaring_a_key_in_both_families_is_a_construction_error():
    """Two answers to "what turns this string into an object" is a mistake."""
    with pytest.raises(ValueError, match="import_strings and resolvers"):
        AppSettings(
            "STAPEL_RESOLV_BOTH",
            defaults={"RESOLV_F": IMPL},
            import_strings=("RESOLV_F",),
            resolvers={"RESOLV_F": Registry.resolve},
        )


def test_a_resolver_key_may_still_be_declared_no_env():
    """no_env closes it harder; the resolver is orthogonal to the env policy."""
    s = AppSettings(
        "STAPEL_RESOLV_NOENV",
        defaults={"RESOLV_G": "memory"},
        resolvers={"RESOLV_G": Registry.resolve},
        no_env=("RESOLV_G",),
    )
    assert s._env_allowed("RESOLV_G") is False
