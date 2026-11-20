"""Declared error params reach errors.json; a re-worded key cannot leave its
owner's translations silently behind.

Two seams, found by a native client's end-to-end run against a scaffolded
backend:

* a 429 answer carries ``retry_after`` (seconds), ``retry_after_display`` and
  ``retry_after_minutes``, but ``errors.json`` listed only what the en
  template printed, untyped — a generated client could not read the wait;
* a package re-registered core's ``error.429.rate_limit`` with new wording,
  without claiming it. Core's ru/es were translated from core's en, so the
  instance served the new en next to the old translation, and no gate saw it:
  core's catalog gate runs where only core's text exists, the package's gate
  checks only the keys it owns.
"""
import pytest

from stapel_core.django.api import errors as core_errors
from stapel_core.django.api.errors import (
    ERR_429_RATE_LIMIT,
    RETRY_PARAM_TYPES,
    build_error_registry,
    error_429_rate_limit,
    error_param_types,
    error_text_overrides,
    register_service_errors,
)
from stapel_core.i18n import check_registry_catalog_pairing, content_hash
from stapel_core.i18n.catalogs import owner_source_hashes

PROBE = "error.409.params_probe"
#: core's en for the 429 before 0.99.0 — the wording core's ru/es came from.
OLD_429_EN = "Too many attempts. Try again in {retry_after_minutes} minutes."


@pytest.fixture
def isolated_registry(monkeypatch):
    for name in ("_GLOBAL_REGISTRY", "_OWNER_REGISTRY", "_PARAM_TYPE_REGISTRY",
                 "_REMEDIATION_REGISTRY", "_TEXT_OVERRIDES", "_LANGUAGE_REGISTRY"):
        current = getattr(core_errors, name)
        monkeypatch.setattr(core_errors, name, {
            k: (dict(v) if isinstance(v, dict) else v) for k, v in current.items()
        })


@pytest.fixture
def core_app_installed():
    """Core's catalogs ship with ``stapel_core.django`` — every service installs it."""
    from django.test import modify_settings

    with modify_settings(INSTALLED_APPS={"append": "stapel_core.django"}):
        yield


def _entry(code):
    (entry,) = [e for e in build_error_registry() if e["code"] == code]
    return entry


# -- declared params ------------------------------------------------------------

def test_declared_params_extend_and_type_the_template(isolated_registry):
    register_service_errors(
        {PROBE: "Taken: at most {limit} per day."},
        param_types={PROBE: {"limit": "integer", "retry_after": "integer"}},
    )
    entry = _entry(PROBE)
    assert entry["params"] == ["limit", "retry_after"]
    assert entry["param_types"] == {"limit": "integer", "retry_after": "integer"}


def test_an_undeclared_code_has_no_param_types_field(isolated_registry):
    register_service_errors({PROBE: "Taken: at most {limit} per day."})
    entry = _entry(PROBE)
    assert entry["params"] == ["limit"]
    assert "param_types" not in entry


def test_the_rate_limit_declares_every_param_its_helper_builds():
    entry = _entry(ERR_429_RATE_LIMIT)
    sent = error_429_rate_limit(90).data["params"]
    assert set(entry["params"]) == set(sent) == set(RETRY_PARAM_TYPES)
    assert entry["param_types"] == {
        "retry_after": "integer",
        "retry_after_display": "string",
        "retry_after_minutes": "integer",
    }
    assert isinstance(sent["retry_after"], int)
    assert isinstance(sent["retry_after_minutes"], int)
    assert isinstance(sent["retry_after_display"], str)


def test_param_types_may_name_an_already_registered_key(isolated_registry):
    register_service_errors({PROBE: "Taken."})
    register_service_errors({}, param_types={PROBE: {"slot": "string"}},
                            remediation={PROBE: "fix_input"})
    entry = _entry(PROBE)
    assert entry["params"] == ["slot"]
    assert entry["remediation"] == "fix_input"
    # a hint for someone else's key no longer needs a copy of its text
    assert PROBE not in error_text_overrides()


@pytest.mark.parametrize("declared, match", [
    ({PROBE: {"limit": "int"}}, "must be one of"),
    ({PROBE: {"not a name": "string"}}, "not a param name"),
    ({PROBE: {}}, "non-empty"),
    ({"error.409.never_registered": {"x": "string"}}, "unknown error key"),
])
def test_bad_declarations_are_refused(isolated_registry, declared, match):
    with pytest.raises(ValueError, match=match):
        register_service_errors({PROBE: "Taken."} if PROBE in declared else {},
                                param_types=declared)


def test_declarations_accumulate_per_code(isolated_registry):
    register_service_errors({PROBE: "Taken."}, param_types={PROBE: {"a": "string"}})
    register_service_errors({}, param_types={PROBE: {"b": "boolean"}})
    assert error_param_types()[PROBE] == {"a": "string", "b": "boolean"}


# -- source drift across packages --------------------------------------------------

def _core_pairing(**kwargs):
    entries = [e for e in build_error_registry() if e["owner"] == "stapel_core"]
    return check_registry_catalog_pairing(entries, **kwargs)


def test_cores_own_catalogs_are_current_against_its_registry(core_app_installed):
    issues = _core_pairing()
    assert {i.language for i in issues} <= {"*"} and not [i for i in issues if i.level == "error"]
    assert owner_source_hashes("stapel_core", "errors", "ru"), "core's catalogs were not seen"


def test_cores_rate_limit_translations_were_made_from_its_current_en(core_app_installed):
    en = core_errors._GLOBAL_REGISTRY[ERR_429_RATE_LIMIT]
    for lang in ("ru", "es"):
        hashes = owner_source_hashes("stapel_core", "errors", lang)
        assert hashes[ERR_429_RATE_LIMIT] == content_hash(en), lang


def test_an_unclaimed_reword_of_a_core_key_is_stale_at_emission(isolated_registry, core_app_installed):
    # what a sibling package did: re-register core's key with other wording
    register_service_errors({ERR_429_RATE_LIMIT: OLD_429_EN})
    assert ERR_429_RATE_LIMIT in error_text_overrides()
    stale = [i for i in _core_pairing() if i.code == "stale"]
    assert sorted(i.language for i in stale) == ["es", "ru"]
    assert all(ERR_429_RATE_LIMIT in i.message for i in stale)
    assert all(error_text_overrides()[ERR_429_RATE_LIMIT] in i.message for i in stale)


def test_re_registering_the_owners_exact_text_is_not_a_reword(isolated_registry, core_app_installed):
    register_service_errors(
        {ERR_429_RATE_LIMIT: core_errors._GLOBAL_REGISTRY[ERR_429_RATE_LIMIT]})
    assert ERR_429_RATE_LIMIT not in error_text_overrides()
    assert [i for i in _core_pairing() if i.code == "stale"] == []


def test_claiming_the_key_clears_the_override(isolated_registry):
    register_service_errors({ERR_429_RATE_LIMIT: OLD_429_EN})
    register_service_errors({ERR_429_RATE_LIMIT: OLD_429_EN}, owner="probe_owner")
    assert ERR_429_RATE_LIMIT not in error_text_overrides()


def test_the_stale_verdict_reads_the_owners_recorded_hash():
    entries = [{"code": PROBE, "status": 409, "params": [], "remediation": "fix_input",
                "en": "New wording.", "owner": "pkg"}]

    def run(recorded):
        return check_registry_catalog_pairing(
            entries,
            languages_of=lambda pkg, domain: {"ru"},
            owner_catalogs=lambda pkg, domain, lang: {PROBE: "Старая формулировка."},
            owner_hashes=lambda pkg, domain, lang: {PROBE: recorded},
            text_overrides={},  # the verdict needs no override record
        )

    assert [i.code for i in run(content_hash("Old wording."))] == ["stale"]
    assert run(content_hash("New wording.")) == []
