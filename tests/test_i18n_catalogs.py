"""stapel_core.i18n — domain-agnostic catalogs, provenance, translate + gate.

Covers the wave-0 mechanism (i18n-shipping.md §1/§2/§5): domain discovery +
later-wins merge, the ``.state.json`` provenance sidecar, ``translate_catalog``
(seed → llm → approve, byte-stable, content-hash cached, stale-invalidation),
and ``check_translation_catalogs`` (missing/stale/params/unstable = E,
unreviewed/orphan = W).
"""
import json

import pytest

from stapel_core.i18n import (
    ORIGIN_HUMAN,
    ORIGIN_LLM,
    StateSidecar,
    check_translation_catalogs,
    content_hash,
    dump_catalog,
    load_app_catalogs,
    summarize,
    translate_catalog,
)
from stapel_core.i18n.catalogs import load_catalog_file
from stapel_core.i18n.conf import i18n_settings, project_languages

SOURCE = {
    "error.400.bad_request": "Bad request",
    "error.429.rate_limit": "Try again in {retry_after_minutes} minutes.",
    "error.404.not_found": "Requested resource not found",
}


class FakeTranslator:
    def __init__(self):
        self.calls = 0

    def translate(self, entries, source_language, target_language):
        self.calls += 1
        return {k: f"[{target_language}] {v}" for k, v in entries.items()}


# ---------------------------------------------------------------------------
# Domain generalization: discovery + later-wins merge (any domain)
# ---------------------------------------------------------------------------

def _write(tmp_path, app, domain, lang, mapping):
    d = tmp_path / app / "translations"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{domain}.{lang}.json").write_text(
        json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return tmp_path / app


def test_load_app_catalogs_generalized_over_domain(tmp_path):
    a = _write(tmp_path, "a", "errors", "ru", {"error.404.not_found": "из-a", "k2": "из-a"})
    b = _write(tmp_path, "b", "errors", "ru", {"k2": "из-b"})
    merged = load_app_catalogs("errors", "ru", dirs=[a, b])
    assert merged == {"error.404.not_found": "из-a", "k2": "из-b"}  # later app wins
    # a different domain in the same dirs is independent
    assert load_app_catalogs("flows", "ru", dirs=[a, b]) == {}


# ---------------------------------------------------------------------------
# Provenance sidecar
# ---------------------------------------------------------------------------

def test_state_sidecar_roundtrip_and_byte_stable(tmp_path):
    s = StateSidecar(tmp_path / ".state.json")
    s.set("errors", "ru", "b.key", source_hash="h2", origin=ORIGIN_LLM)
    s.set("errors", "ru", "a.key", source_hash="h1", origin="seed:x")
    s.save()
    raw = (tmp_path / ".state.json").read_text()
    # nested keys sorted, trailing newline
    assert raw.endswith("\n")
    assert raw.index('"a.key"') < raw.index('"b.key"')
    reloaded = StateSidecar(tmp_path / ".state.json")
    assert reloaded.get("errors", "ru", "a.key") == {"hash": "h1", "origin": "seed:x"}
    # re-render is identical (byte-stable)
    assert StateSidecar(tmp_path / ".state.json").render() == raw


# ---------------------------------------------------------------------------
# translate_catalog: seed / kept / llm / approve
# ---------------------------------------------------------------------------

def test_seed_fills_and_records_provenance(tmp_path):
    seed = {"error.400.bad_request": "Некорректный запрос", "unrelated": "x"}
    r = translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE,
                          seed=seed, seed_label="stapel-builtin")
    assert r.seeded == 1 and r.written
    catalog = load_catalog_file(tmp_path / "errors.ru.json")
    assert catalog["error.400.bad_request"] == "Некорректный запрос"
    assert "unrelated" not in catalog  # keys outside the registry ignored
    st = StateSidecar(tmp_path / ".state.json").get("errors", "ru", "error.400.bad_request")
    assert st["origin"] == "seed:stapel-builtin"
    # two keys nothing filled → reported missing (gate will fail)
    assert set(r.missing) == {"error.429.rate_limit", "error.404.not_found"}


def test_kept_is_idempotent_zero_diff(tmp_path):
    seed = {k: f"ru:{v}" for k, v in SOURCE.items()}
    r1 = translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, seed=seed)
    assert r1.seeded == 3 and not r1.missing
    first = (tmp_path / "errors.ru.json").read_bytes()
    # re-run without changes: everything kept, nothing written
    r2 = translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, seed=seed)
    assert r2.kept == 3 and r2.seeded == 0 and not r2.written
    assert (tmp_path / "errors.ru.json").read_bytes() == first


def test_llm_fills_remainder_cached_and_stable(tmp_path):
    seed = {"error.400.bad_request": "Некорректный запрос"}
    tr = FakeTranslator()
    r = translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE,
                          seed=seed, llm=True, translator=tr)
    assert tr.calls == 1 and r.seeded == 1 and r.translated == 2 and not r.missing
    catalog = load_catalog_file(tmp_path / "errors.ru.json")
    assert catalog["error.404.not_found"] == "[ru] Requested resource not found"
    st = StateSidecar(tmp_path / ".state.json")
    assert st.get("errors", "ru", "error.404.not_found")["origin"] == ORIGIN_LLM
    first = (tmp_path / "errors.ru.json").read_bytes()
    # re-run: everything fresh → kept, zero LLM calls, zero diff
    r2 = translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE,
                           seed=seed, llm=True, translator=tr)
    assert tr.calls == 1 and r2.kept == 3 and r2.translated == 0 and not r2.written
    assert (tmp_path / "errors.ru.json").read_bytes() == first


def test_source_change_invalidates_only_that_key(tmp_path):
    seed = {k: f"ru:{v}" for k, v in SOURCE.items()}
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, seed=seed)
    changed = dict(SOURCE)
    changed["error.404.not_found"] = "Resource not found (edited)"
    tr = FakeTranslator()
    r = translate_catalog("errors", "ru", tmp_path, source_texts=changed,
                          llm=True, translator=tr)
    # only the edited key is re-translated; the other two stay kept
    assert r.kept == 2 and r.translated == 1
    st = StateSidecar(tmp_path / ".state.json").get("errors", "ru", "error.404.not_found")
    assert st["hash"] == content_hash("Resource not found (edited)")


def test_approve_flips_origin_to_human_without_retranslating(tmp_path):
    tr = FakeTranslator()
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, llm=True, translator=tr)
    assert tr.calls == 1
    r = translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE,
                          approve=["error.400.bad_request"])
    assert r.approved == 1
    st = StateSidecar(tmp_path / ".state.json")
    assert st.get("errors", "ru", "error.400.bad_request")["origin"] == ORIGIN_HUMAN
    assert st.get("errors", "ru", "error.404.not_found")["origin"] == ORIGIN_LLM
    # approve-all flips the rest
    r2 = translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, approve_all=True)
    assert r2.approved == 3
    st2 = StateSidecar(tmp_path / ".state.json")
    assert all(st2.get("errors", "ru", k)["origin"] == ORIGIN_HUMAN for k in SOURCE)


def test_catalog_written_byte_stable(tmp_path):
    seed = {k: f"ru:{v}" for k, v in SOURCE.items()}
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, seed=seed)
    catalog = load_catalog_file(tmp_path / "errors.ru.json")
    assert (tmp_path / "errors.ru.json").read_text() == dump_catalog(catalog)


# ---------------------------------------------------------------------------
# check_translation_catalogs — the gate
# ---------------------------------------------------------------------------

def _gate(tmp_path, **kw):
    return check_translation_catalogs(
        "errors", tmp_path, source_texts=SOURCE, languages=["en", "ru"], **kw)


def test_gate_flags_missing_key(tmp_path):
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE,
                      seed={"error.400.bad_request": "Некорректный запрос"})
    issues = _gate(tmp_path)
    missing = [i for i in issues if i.code == "missing"]
    assert {i.message.split()[-1].strip("'") for i in missing} >= {
        "error.429.rate_limit", "error.404.not_found"} or len(missing) == 2
    assert all(i.level == "error" for i in missing)


def test_gate_flags_stale_after_source_edit(tmp_path):
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE,
                      seed={k: f"ru:{v}" for k, v in SOURCE.items()})
    edited = dict(SOURCE, **{"error.400.bad_request": "Bad request (v2)"})
    issues = check_translation_catalogs(
        "errors", tmp_path, source_texts=edited, languages=["ru"])
    stale = [i for i in issues if i.code == "stale"]
    assert len(stale) == 1 and stale[0].level == "error"


def test_gate_flags_params_mismatch(tmp_path):
    # translation drops the {retry_after_minutes} placeholder → E
    bad_seed = {
        "error.400.bad_request": "Некорректный запрос",
        "error.429.rate_limit": "Попробуйте позже.",  # placeholder lost
        "error.404.not_found": "Ресурс не найден",
    }
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, seed=bad_seed)
    issues = check_translation_catalogs(
        "errors", tmp_path, source_texts=SOURCE, languages=["ru"])
    params = [i for i in issues if i.code == "params"]
    assert len(params) == 1 and params[0].level == "error"
    assert "error.429.rate_limit" in params[0].message


def test_gate_flags_non_byte_stable_file(tmp_path):
    (tmp_path / "errors.ru.json").write_text(
        '{"error.400.bad_request": "x", "error.429.rate_limit": "{retry_after_minutes}", '
        '"error.404.not_found": "y"}', encoding="utf-8")
    issues = check_translation_catalogs(
        "errors", tmp_path, source_texts=SOURCE, languages=["ru"])
    assert any(i.code == "unstable" and i.level == "error" for i in issues)


def test_gate_counts_unreviewed_as_warning_and_clean_when_approved(tmp_path):
    tr = FakeTranslator()
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, llm=True, translator=tr)
    issues = check_translation_catalogs(
        "errors", tmp_path, source_texts=SOURCE, languages=["ru"])
    errors, warnings = summarize(issues)
    assert errors == 0 and warnings == 3  # 3 llm-origin unreviewed
    assert all(i.level == "warning" for i in issues)
    # approve → no more warnings
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, approve_all=True)
    e2, w2 = summarize(check_translation_catalogs(
        "errors", tmp_path, source_texts=SOURCE, languages=["ru"]))
    assert e2 == 0 and w2 == 0


def test_gate_orphan_is_warning_not_error(tmp_path):
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE,
                      seed={k: f"ru:{v}" for k, v in SOURCE.items()})
    # host override for a key from another module
    catalog = load_catalog_file(tmp_path / "errors.ru.json")
    catalog["error.999.foreign"] = "чужой ключ"
    (tmp_path / "errors.ru.json").write_text(dump_catalog(catalog), encoding="utf-8")
    issues = check_translation_catalogs(
        "errors", tmp_path, source_texts=SOURCE, languages=["ru"])
    orphan = [i for i in issues if i.code == "orphan"]
    assert len(orphan) == 1 and orphan[0].level == "warning"


# ---------------------------------------------------------------------------
# Soft delegation DOC_LANGUAGES ← LOCALES (open question #6)
# ---------------------------------------------------------------------------

def test_project_languages_defaults_to_locales():
    i18n_settings.reload()
    assert project_languages() == ["en", "ru"]


@pytest.mark.django_db
def test_locales_setting_drives_project_languages(settings):
    settings.STAPEL_I18N = {"LOCALES": ["en", "ru", "es"]}
    i18n_settings.reload()
    assert project_languages() == ["en", "ru", "es"]


def test_explicit_doc_languages_wins_over_locales(settings):
    settings.STAPEL_I18N = {"LOCALES": ["en", "ru", "es"]}
    settings.STAPEL_FLOWS = {"DOC_LANGUAGES": ["en", "fr"]}
    i18n_settings.reload()
    assert project_languages() == ["en", "fr"]
