"""A settings list supplied by the environment silently became garbage.

``AppSettings._raw`` handed back ``os.environ.get(key)`` as a raw string with
no parsing. ``DATA_OWNERS=auth,profiles`` was then iterated CHARACTER BY
CHARACTER — a dozen data owners named ``a``, ``u``, ``t``, ``h``, ``,`` — and
since every character is a ``str``, every downstream type check passed. The
GDPR data-owner check went green and erasure was certified against nonsense.

The rule: a key whose DECLARED SHAPE is a container must not be silently
accepted from a bare environment string. It is refused, by name, with the
reason — see ``_STRUCTURED_TYPES`` in ``stapel_core.conf`` for why refusing
beats parsing a format nobody declared.
"""
import pytest
from django.core.checks import Error
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from stapel_core.conf import AppSettings
from stapel_core.conf_checks import E002_STRUCTURED_ENV_VAR, check_structured_env_vars


@pytest.fixture
def owners():
    return AppSettings(
        "STAPEL_TESTGDPR",
        defaults={
            "DATA_OWNERS": [],
            "DATA_OWNERS_VERSION": "",
            "OWNER_TIMEOUT_HOURS": 24,
            "ROUTING": {},
            "TAGS": (),
        },
    )


# ---------------------------------------------------------------------------
# the read is refused
# ---------------------------------------------------------------------------


def test_a_list_key_from_a_bare_env_string_is_refused(owners, monkeypatch):
    monkeypatch.setenv("DATA_OWNERS", "auth,profiles")
    with pytest.raises(ImproperlyConfigured) as exc:
        owners.DATA_OWNERS
    message = str(exc.value)
    assert "DATA_OWNERS" in message, "the refusal must name the key"
    assert "list" in message, "the refusal must name the declared shape"
    assert "auth,profiles" in message, "the refusal must show what was supplied"
    assert "CHARACTER BY CHARACTER" in message, "the refusal must give the reason"


def test_the_silent_garbage_is_what_used_to_happen(owners, monkeypatch):
    """Pins the shape of the defect, so the refusal is never quietly relaxed
    back into a passthrough."""
    monkeypatch.setenv("DATA_OWNERS", "auth,profiles")
    with pytest.raises(ImproperlyConfigured):
        list(owners.DATA_OWNERS)
    # what a passthrough would have produced, spelled out
    assert len(list("auth,profiles")) == 13
    assert all(isinstance(c, str) for c in "auth,profiles")


def test_dict_and_tuple_shapes_are_refused_too(owners, monkeypatch):
    monkeypatch.setenv("ROUTING", "a:b")
    monkeypatch.setenv("TAGS", "x,y")
    with pytest.raises(ImproperlyConfigured):
        owners.ROUTING
    with pytest.raises(ImproperlyConfigured):
        owners.TAGS


def test_scalar_keys_still_read_from_the_environment(owners, monkeypatch):
    """The refusal is scoped to declared containers. A scalar IS what the
    environment is for, and closing that would be a different change."""
    monkeypatch.setenv("DATA_OWNERS_VERSION", "2026-08-13.1")
    monkeypatch.setenv("OWNER_TIMEOUT_HOURS", "6")
    assert owners.DATA_OWNERS_VERSION == "2026-08-13.1"
    assert owners.OWNER_TIMEOUT_HOURS == "6"


def test_the_settings_dict_is_the_way_in(owners, monkeypatch):
    """Refusing the environment must not refuse the value — the namespace
    dict (what the scaffold emits) is unaffected."""
    monkeypatch.setenv("DATA_OWNERS", "auth,profiles")
    with override_settings(
        STAPEL_TESTGDPR={"DATA_OWNERS": ["auth", {"name": "cdn", "kind": "remote"}]}
    ):
        owners.reload()
        assert owners.DATA_OWNERS == ["auth", {"name": "cdn", "kind": "remote"}]
    owners.reload()


def test_a_flat_django_setting_still_wins(owners, monkeypatch):
    monkeypatch.setenv("DATA_OWNERS", "auth,profiles")
    with override_settings(DATA_OWNERS=["auth", "profiles"]):
        owners.reload()
        assert owners.DATA_OWNERS == ["auth", "profiles"]
    owners.reload()


def test_an_unset_env_var_leaves_the_default_alone(owners, monkeypatch):
    monkeypatch.delenv("DATA_OWNERS", raising=False)
    assert owners.DATA_OWNERS == []


def test_a_container_key_closed_to_the_environment_is_not_double_reported(monkeypatch):
    """``no_env`` already ignores the variable — W001's territory, not this
    rule's. Refusing a read that never consults the environment would be a
    false alarm."""
    ns = AppSettings("STAPEL_TESTNOENV", defaults={"OWNERS": []}, no_env=("OWNERS",))
    monkeypatch.setenv("OWNERS", "auth,profiles")
    assert ns.OWNERS == []
    assert ns.structured_env_vars() == []


# ---------------------------------------------------------------------------
# and it is found at boot, not at first read
# ---------------------------------------------------------------------------


def test_the_system_check_reports_it_at_boot(owners, monkeypatch):
    """A lazy refusal fires the first time some code path reads the key —
    possibly days in, possibly never. The deploy gate is the right place."""
    monkeypatch.setenv("DATA_OWNERS", "auth,profiles")
    findings = [
        f for f in check_structured_env_vars()
        if f.id == E002_STRUCTURED_ENV_VAR and "STAPEL_TESTGDPR" in f.msg
    ]
    assert len(findings) == 1
    assert isinstance(findings[0], Error), "the process is running garbage, not a safe default"
    assert "DATA_OWNERS" in findings[0].msg
    assert "list" in findings[0].msg


def test_the_check_is_silent_when_nothing_is_set(owners, monkeypatch):
    monkeypatch.delenv("DATA_OWNERS", raising=False)
    monkeypatch.delenv("ROUTING", raising=False)
    monkeypatch.delenv("TAGS", raising=False)
    assert [
        f for f in check_structured_env_vars() if "STAPEL_TESTGDPR" in f.msg
    ] == []


def test_structured_keys_are_derived_from_the_declared_defaults(owners):
    assert owners.structured_keys() == ["DATA_OWNERS", "ROUTING", "TAGS"]
