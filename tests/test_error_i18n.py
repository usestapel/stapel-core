"""Core's own localized error catalogs — the 41 cross-cutting keys it owns.

i18n-shipping.md §5. Core registers the error keys every service inherits:
:data:`COMMON_ERRORS` (HTTP-status generics + the ``error.400.field.*``
validation family), the verification step-up keys, and the captcha/network
keys. Until now it shipped **no** ``translations/`` directory at all, while
``CommonDjangoConfig`` sat in every project's INSTALLED_APPS — so the loader
had a slot for a core catalog and nothing to put in it, and every module that
localized its own errors re-translated all 41 keys to satisfy the coverage
gate. Five libraries, two languages: 410 duplicated entries, byte-identical,
none of them an intentional reword.

The catalogs live in the ``stapel_core.django`` app package (the app config the
loader walks), and they are generated exactly like a module's: seeded from the
already-paid-for ``stapel-translate`` builtin corpus, with the handful of keys
the corpus does not carry recorded here as machine translations.

Regenerate after adding/changing a core error key or a translation:

    STAPEL_REGEN_ERROR_I18N=1 python -m pytest tests/test_error_i18n.py::test_regen

then commit ``django/translations/errors.<lang>.json`` + ``.state.json``.
Without the env var the same module is the CI gate.
"""
import json
import os
from pathlib import Path

import pytest

from stapel_core.i18n import (
    check_translation_catalogs,
    source_owners,
    source_texts,
    summarize,
    translate_catalog,
)
from stapel_core.i18n.catalogs import load_catalog_file

REPO = Path(__file__).resolve().parent.parent
#: The `stapel_core.django` app package — what `CommonDjangoConfig.path` is, and
#: therefore the only directory under core the catalog loader ever opens.
TRANSLATIONS = REPO / "django" / "translations"
LANGUAGES = ["en", "ru", "es"]
TARGET_LANGUAGES = [lang for lang in LANGUAGES if lang != "en"]

#: The package that owns every key core registers.
OWNER = "stapel_core"

#: stapel-translate builtin fixtures (the curated seed corpus). Overridable for
#: an out-of-tree checkout via STAPEL_TRANSLATE_FIXTURES.
_FIXTURES = Path(
    os.environ.get(
        "STAPEL_TRANSLATE_FIXTURES",
        REPO.parent / "stapel-translate" / "fixtures" / "builtin",
    )
)

#: Machine translations (origin: llm) of the core keys the builtin corpus does
#: not carry. Same wording the modules already ship for these two keys — the
#: point of moving them here is that there is now one copy, not five.
_MACHINE_RU = {
    "error.403.network_blocked":
        "Запросы из этой сети не разрешены.",
    "error.403.verification_enrollment_required":
        "Требуется регистрация фактора подтверждения.",
}

_MACHINE_ES = {
    "error.403.network_blocked":
        "No se permiten solicitudes desde esta red.",
    "error.403.verification_enrollment_required":
        "Es necesario registrar un factor de verificación.",
}

_MACHINE = {"ru": _MACHINE_RU, "es": _MACHINE_ES}


class _DictTranslator:
    """Offline translator seam — returns fixed machine translations by key."""

    def __init__(self, table):
        self._table = table

    def translate(self, entries, source_language, target_language):
        return {k: self._table[k] for k in entries if k in self._table}


def _seed_from_fixtures(lang: str) -> dict[str, str]:
    path = _FIXTURES / f"{lang}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and k.startswith("error.")
        and isinstance(v, str) and v
    }


def _regen(lang: str):
    return translate_catalog(
        "errors", lang, TRANSLATIONS,
        source_texts=source_texts("errors"),
        # `stapel_core.django` is not an installed app in core's own test
        # config, so ownership cannot be inferred from the directory — state it.
        owner=OWNER,
        owners=source_owners("errors"),
        seed=_seed_from_fixtures(lang),
        seed_label="stapel-builtin",
        llm=True,
        translator=_DictTranslator(_MACHINE.get(lang, {})),
    )


def test_regen():
    """Regenerate (env-gated) or assert every catalog is a no-op regen (drift)."""
    if os.environ.get("STAPEL_REGEN_ERROR_I18N"):
        for lang in TARGET_LANGUAGES:
            result = _regen(lang)
            assert not result.missing, f"{lang}: still missing: {result.missing}"
        return

    for lang in TARGET_LANGUAGES:
        path = TRANSLATIONS / f"errors.{lang}.json"
        before = path.read_bytes()
        _regen(lang)
        assert path.read_bytes() == before, (
            f"errors.{lang}.json drifted — run "
            f"STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen"
        )


def test_catalog_gate_green():
    """E: missing / foreign / stale / params / byte-stability — all zero."""
    issues = check_translation_catalogs(
        "errors", TRANSLATIONS,
        source_texts=source_texts("errors"),
        languages=LANGUAGES,
        owner=OWNER,
        owners=source_owners("errors"),
    )
    blocking = [i for i in issues if i.level == "error"]
    assert not blocking, "\n".join(f"[{i.code}] {i.message}" for i in blocking)
    assert summarize(issues)[0] == 0


@pytest.mark.parametrize("lang", TARGET_LANGUAGES)
def test_every_core_key_is_covered(lang):
    """Coverage is the whole point: a module must not need to fill a gap here."""
    catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
    missing = [k for k in source_texts("errors") if k not in catalog]
    assert not missing, f"{lang} catalog missing {len(missing)} key(s): {missing[:8]}"


@pytest.mark.parametrize("lang", TARGET_LANGUAGES)
def test_catalog_holds_exactly_core_owned_keys(lang):
    """Core ships what core owns — no more (it would shadow a module's key)."""
    catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
    owners = source_owners("errors")
    assert set(catalog) == {k for k, o in owners.items() if o == OWNER}


def test_every_core_key_has_an_owner():
    """Ownership is what the gate scopes by — an unattributed key defeats it."""
    owners = source_owners("errors")
    unowned = [k for k in source_texts("errors") if owners.get(k) != OWNER]
    assert not unowned, f"core keys with no core ownership: {unowned}"
