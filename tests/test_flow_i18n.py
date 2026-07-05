"""Flow i18n (flow-system.md §2): keys, catalogs, resolution chain, cache."""
import json

import pytest

from stapel_core.comm import register_function
from stapel_core.comm.registry import function_registry
from stapel_core.flows import (
    Flow,
    flow_registry,
    flow_step,
    flow_source_texts,
    load_app_catalogs,
    resolve_flow_texts,
)
from stapel_core.flows.checks import check_flows
from stapel_core.flows.docs import export_json, render_flow_markdown, render_index_markdown
from stapel_core.flows.i18n import CommDocTranslator, DocTranslationCache


@pytest.fixture(autouse=True)
def clean_registries():
    flow_registry.clear()
    function_registry.clear()
    yield
    flow_registry.clear()
    function_registry.clear()


def _make_flow(flow_id="test.i18n", **kwargs):
    defaults = dict(
        title="Test scenario",
        description="A long enough scenario description to satisfy the "
                    "completeness check minimum length threshold.",
        actors=["User"],
    )
    defaults.update(kwargs)
    flow = Flow(flow_id, **defaults)
    flow.human(order=0, note="User enters their email")
    flow.action("user.registered", order=2, note="Emitted on first login")
    return flow


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def test_implicit_keys_derived_from_flow_id_and_order():
    flow = _make_flow()
    assert flow.title_key == "flow.test.i18n.title"
    assert flow.description_key == "flow.test.i18n.description"
    steps = flow.sorted_steps()
    assert steps[0].note_key == "flow.test.i18n.step.0.note"
    assert steps[1].note_key == "flow.test.i18n.step.2.note"


def test_explicit_keys_win():
    flow = Flow(
        "test.explicit",
        title="T", description="d" * 50,
        title_key="custom.title", description_key="custom.desc",
    )
    flow.human(order=0, note="n", note_key="custom.step")
    assert flow.title_key == "custom.title"
    assert flow.description_key == "custom.desc"
    assert flow.steps[0].note_key == "custom.step"


def test_flow_step_decorator_derives_and_annotates_note_key():
    flow = _make_flow()

    @flow_step(flow, order=1, note="Request the code")
    def post(self, request):  # pragma: no cover - never called
        pass

    step = [s for s in flow.steps if s.kind == "http"][0]
    assert step.note_key == "flow.test.i18n.step.1.note"
    assert post._stapel_flows[0]["note_key"] == "flow.test.i18n.step.1.note"


def test_flow_source_texts_maps_every_key():
    flow = _make_flow()
    texts = flow_source_texts([flow])
    assert texts["flow.test.i18n.title"] == "Test scenario"
    assert texts["flow.test.i18n.step.0.note"] == "User enters their email"
    assert len(texts) == 4  # title + description + 2 notes


def test_duplicate_implicit_note_keys_flagged_by_check_flows():
    flow = _make_flow()
    flow.human(order=0, note="Second step reusing order 0")
    issues = check_flows()
    messages = "\n".join(i.message for i in issues if i.level == "error")
    assert "share the i18n key" in messages
    assert "flow.test.i18n.step.0.note" in messages


# ---------------------------------------------------------------------------
# flows.json / markdown
# ---------------------------------------------------------------------------

def test_export_json_carries_keys_and_literals():
    flow = _make_flow()
    data = json.loads(export_json([flow], {}))
    entry = data[0]
    assert entry["title_key"] == "flow.test.i18n.title"
    assert entry["description_key"] == "flow.test.i18n.description"
    assert entry["title"] == "Test scenario"  # canonical literal kept
    assert entry["steps"][0]["note_key"] == "flow.test.i18n.step.0.note"
    assert entry["steps"][0]["note"] == "User enters their email"


def test_render_markdown_resolves_texts_with_literal_fallback():
    flow = _make_flow()
    texts = {
        "flow.test.i18n.title": "Тестовый сценарий",
        "flow.test.i18n.step.0.note": "Пользователь вводит email",
        # description key intentionally missing -> literal fallback
    }
    md = render_flow_markdown(flow, {}, texts=texts)
    assert "# Тестовый сценарий" in md
    assert "Пользователь вводит email" in md
    assert "long enough scenario description" in md
    idx = render_index_markdown([flow], {}, texts=texts)
    assert "Тестовый сценарий" in idx
    # without texts everything renders from literals (backward compat)
    assert "# Test scenario" in render_flow_markdown(flow, {})


# ---------------------------------------------------------------------------
# Catalogs
# ---------------------------------------------------------------------------

def _write_catalog(tmp_path, appname, lang, mapping):
    d = tmp_path / appname / "translations"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"flows.{lang}.json").write_text(
        json.dumps(mapping, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path / appname


def test_load_app_catalogs_merges_later_apps_win(tmp_path):
    a = _write_catalog(tmp_path, "a", "ru", {"k1": "из-a", "k2": "из-a"})
    b = _write_catalog(tmp_path, "b", "ru", {"k2": "из-b", "k3": "", "k4": 7})
    merged = load_app_catalogs("ru", dirs=[a, b])
    # later app wins on collision; empty / non-string values are dropped
    assert merged == {"k1": "из-a", "k2": "из-b"}
    # missing file for another language is simply absent
    assert load_app_catalogs("de", dirs=[a, b]) == {}


def test_load_app_catalogs_tolerates_broken_files(tmp_path):
    d = tmp_path / "x" / "translations"
    d.mkdir(parents=True)
    (d / "flows.ru.json").write_text("not json", encoding="utf-8")
    assert load_app_catalogs("ru", dirs=[tmp_path / "x"]) == {}


# ---------------------------------------------------------------------------
# Resolution chain
# ---------------------------------------------------------------------------

def test_resolve_none_language_returns_literals():
    flow = _make_flow()
    texts = resolve_flow_texts([flow], None, catalog_dirs=[])
    assert texts == flow_source_texts([flow])


def test_resolve_catalog_beats_translate_function(tmp_path):
    flow = _make_flow()
    app = _write_catalog(tmp_path, "app", "ru", {
        "flow.test.i18n.title": "Каталожный заголовок",
    })
    seen = {}

    def fake_resolve(payload):
        seen["keys"] = payload["keys"]
        return {"values": {
            "flow.test.i18n.title": "ИЗ БД — не должен победить",
            "flow.test.i18n.step.0.note": "Из БД",
            "unrelated.key": "мусор",
        }}

    register_function("translate.resolve", fake_resolve)
    texts = resolve_flow_texts([flow], "ru", catalog_dirs=[app])
    assert texts["flow.test.i18n.title"] == "Каталожный заголовок"
    assert texts["flow.test.i18n.step.0.note"] == "Из БД"
    assert "unrelated.key" not in texts
    # only keys the catalogs did not cover were asked from translate
    assert "flow.test.i18n.title" not in seen["keys"]
    # keys missing everywhere fall back to the literal
    assert texts["flow.test.i18n.step.2.note"] == "Emitted on first login"


def test_resolve_survives_missing_translate_provider():
    flow = _make_flow()
    texts = resolve_flow_texts([flow], "de", catalog_dirs=[])
    assert texts == flow_source_texts([flow])


class CountingTranslator:
    def __init__(self):
        self.calls = 0

    def translate(self, entries, source_language, target_language):
        self.calls += 1
        return {k: f"[{target_language}] {v}" for k, v in entries.items()}


def test_llm_translator_with_content_hash_cache_is_byte_stable(tmp_path):
    flow = _make_flow()
    cache_path = tmp_path / "flow-i18n-cache.de.json"
    translator = CountingTranslator()

    texts = resolve_flow_texts(
        [flow], "de", catalog_dirs=[], use_translate_function=False,
        llm=True, cache_path=cache_path, translator=translator,
    )
    assert translator.calls == 1
    assert texts["flow.test.i18n.title"] == "[de] Test scenario"
    first_bytes = cache_path.read_bytes()

    # regeneration without source changes: zero LLM calls, zero diff
    texts2 = resolve_flow_texts(
        [flow], "de", catalog_dirs=[], use_translate_function=False,
        llm=True, cache_path=cache_path, translator=translator,
    )
    assert translator.calls == 1
    assert texts2 == texts
    assert cache_path.read_bytes() == first_bytes


def test_cache_invalidated_by_source_change(tmp_path):
    cache = DocTranslationCache(tmp_path / "c.json")
    cache.put("k", "source v1", "перевод v1")
    assert cache.save() is True
    assert cache.save() is False  # unchanged -> no write

    reloaded = DocTranslationCache(tmp_path / "c.json")
    assert reloaded.get("k", "source v1") == "перевод v1"
    assert reloaded.get("k", "source v2") is None  # hash mismatch


def test_llm_disabled_keys_stay_literal(tmp_path):
    flow = _make_flow()
    translator = CountingTranslator()
    texts = resolve_flow_texts(
        [flow], "de", catalog_dirs=[], use_translate_function=False,
        translator=translator,
    )
    assert translator.calls == 0
    assert texts == flow_source_texts([flow])


# ---------------------------------------------------------------------------
# DOC_TRANSLATOR default (llm.translate by comm name)
# ---------------------------------------------------------------------------

def test_comm_doc_translator_calls_llm_translate():
    def fake_llm_translate(payload):
        assert payload["to"] == "de"
        assert payload["from_lang"] == "en"
        return {"status": "ok", "result": {k: f"de:{v}" for k, v in payload["entries"].items()}}

    register_function("llm.translate", fake_llm_translate)
    out = CommDocTranslator().translate({"k": "text"}, "en", "de")
    assert out == {"k": "de:text"}


def test_comm_doc_translator_swallows_failures():
    # no provider registered at all
    assert CommDocTranslator().translate({"k": "text"}, "en", "de") == {}

    register_function("llm.translate", lambda payload: {"status": "failure", "reason": "no key"})
    assert CommDocTranslator().translate({"k": "text"}, "en", "de") == {}


def test_doc_translator_seam_default_resolves():
    from stapel_core.flows.conf import flows_settings

    flows_settings.reload()
    assert flows_settings.DOC_TRANSLATOR is CommDocTranslator
    assert flows_settings.DOC_SOURCE_LANGUAGE == "en"
