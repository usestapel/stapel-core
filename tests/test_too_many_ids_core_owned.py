"""``error.400.too_many_ids`` is core's: a bounded id list is generic.

Two packages each registered the key with their own en text; in an instance
that installs both, the later registration rewrote the other's source text
without claiming it and the stale-translation gate fired. Core now owns the
key — one text, typed ``requested``/``limit`` — and packages raise it without
re-registering it.
"""
import pytest

from stapel_core.django.api import errors as core_errors
from stapel_core.django.api.errors import (
    CORE_OWNER,
    ERR_400_TOO_MANY_IDS,
    build_error_registry,
    error_owners,
    error_text_overrides,
    register_service_errors,
)
from stapel_core.django.errors import ERR_400_TOO_MANY_IDS as SHIM_CONSTANT
from stapel_core.i18n.catalogs import content_hash, load_catalog_file

CORE_EN = "Too many ids: {requested} requested, at most {limit} per batch request"


@pytest.fixture
def isolated_registry(monkeypatch):
    for name in ("_GLOBAL_REGISTRY", "_OWNER_REGISTRY", "_PARAM_TYPE_REGISTRY",
                 "_REMEDIATION_REGISTRY", "_TEXT_OVERRIDES", "_LANGUAGE_REGISTRY"):
        current = getattr(core_errors, name)
        monkeypatch.setattr(core_errors, name, {
            k: (dict(v) if isinstance(v, dict) else v) for k, v in current.items()
        })


def _entry(code):
    (entry,) = [e for e in build_error_registry() if e["code"] == code]
    return entry


def test_core_owns_the_key_with_typed_params():
    assert SHIM_CONSTANT == ERR_400_TOO_MANY_IDS == "error.400.too_many_ids"
    assert error_owners()[ERR_400_TOO_MANY_IDS] == CORE_OWNER
    entry = _entry(ERR_400_TOO_MANY_IDS)
    assert entry["en"] == CORE_EN
    assert entry["status"] == 400
    assert entry["remediation"] == "fix_input"
    assert sorted(entry["params"]) == ["limit", "requested"]
    assert entry["param_types"] == {"limit": "integer", "requested": "integer"}


def test_core_catalogs_translate_the_current_text():
    from pathlib import Path

    import stapel_core.django as core_django

    root = Path(core_django.__file__).parent / "translations"
    for lang in ("ru", "es"):
        catalog = load_catalog_file(root / f"errors.{lang}.json")
        assert "{requested}" in catalog[ERR_400_TOO_MANY_IDS]
        assert "{limit}" in catalog[ERR_400_TOO_MANY_IDS]
    import json

    state = json.loads((root / ".state.json").read_text())
    for domain in ("errors.ru", "errors.es"):
        assert state[domain][ERR_400_TOO_MANY_IDS]["hash"] == content_hash(CORE_EN)


def test_a_package_registering_the_same_text_is_not_an_override(
    isolated_registry, monkeypatch
):
    monkeypatch.setattr(core_errors, "_infer_owner", lambda: "stapel_profiles")
    register_service_errors({ERR_400_TOO_MANY_IDS: CORE_EN})
    assert ERR_400_TOO_MANY_IDS not in error_text_overrides()
    assert error_owners()[ERR_400_TOO_MANY_IDS] == CORE_OWNER


def test_a_package_rewording_it_is_reported(isolated_registry, monkeypatch):
    monkeypatch.setattr(core_errors, "_infer_owner", lambda: "stapel_notifications")
    register_service_errors({ERR_400_TOO_MANY_IDS: "Too many ids in one request."})
    assert error_text_overrides()[ERR_400_TOO_MANY_IDS] == "stapel_notifications"
