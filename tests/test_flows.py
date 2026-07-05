"""Flows engine: registry, decorator, doc rendering, completeness checks."""
import pytest
from django.test import override_settings
from rest_framework.views import APIView

from stapel_core.flows import Flow, flow_registry, flow_step
from stapel_core.flows.checks import check_flows
from stapel_core.flows.docs import (
    endpoint_index,
    export_json,
    render_flow_markdown,
    render_index_markdown,
)


@pytest.fixture(autouse=True)
def clean_registry():
    flow_registry.clear()
    yield
    flow_registry.clear()


def _make_flow(**kwargs):
    defaults = dict(
        title="Тестовый сценарий",
        description="Достаточно длинное описание сценария, чтобы пройти порог "
                    "минимальной длины проверки полноты документации.",
        actors=["Пользователь"],
    )
    defaults.update(kwargs)
    return Flow("test.scenario", **defaults)


def test_flow_registers_and_orders_steps():
    flow = _make_flow()
    flow.action("user.deleted", order=3, note="эмитится после")
    flow.human(order=0, note="вводит email")
    flow.function("cdn.media_exists", order=2, note="проверка")
    kinds = [s.kind for s in flow.sorted_steps()]
    assert kinds == ["human", "function", "action"]
    assert flow_registry.get("test.scenario") is flow


def test_duplicate_flow_id_rejected():
    _make_flow()
    with pytest.raises(ValueError):
        Flow("test.scenario", title="x", description="y" * 50)


def test_flow_step_annotates_and_supports_multiple_flows():
    flow_a = _make_flow()
    flow_b = Flow("test.other", title="Другой", description="d" * 50)

    class DemoView(APIView):
        @flow_step(flow_a, order=1, note="шаг A")
        @flow_step(flow_b, order=2, note="шаг B")
        def post(self, request):  # pragma: no cover - never called
            pass

    memberships = {m["flow"] for m in DemoView.post._stapel_flows}
    assert memberships == {"test.scenario", "test.other"}
    assert any(s.ref.endswith("DemoView.post") for s in flow_a.steps)
    assert any(s.ref.endswith("DemoView.post") for s in flow_b.steps)


def test_markdown_and_json_render():
    flow = _make_flow()
    flow.human(order=0, note="вводит email")
    flow.action("user.registered", order=2, note="создаются профили")
    md = render_flow_markdown(flow, {})
    assert "# Тестовый сценарий" in md
    # renderer chrome defaults to English (DOC_SOURCE_LANGUAGE); content
    # literals are unaffected. Russian chrome is opt-in via language="ru".
    assert "User action" in md
    assert "`user.registered`" in md
    assert "```mermaid" in md  # SA-doc carries a GitHub-native step diagram
    md_ru = render_flow_markdown(flow, {}, language="ru")
    assert "Действие пользователя" in md_ru
    idx = render_index_markdown([flow], {})
    assert "test.scenario" in idx
    data = export_json([flow], {})
    assert '"test.scenario"' in data


def test_check_flows_flags_stub_description_and_empty_notes():
    flow = Flow("test.bad", title="", description="short")
    flow.human(order=1, note="")
    issues = check_flows()
    messages = "\n".join(i.message for i in issues)
    assert "empty title" in messages
    assert "description shorter" in messages
    assert "empty note" in messages


@override_settings(ROOT_URLCONF="tests.flow_urls")
def test_endpoint_coverage_check_and_index():
    import tests.flow_urls as flow_urls

    flow = _make_flow()
    # attach the existing routed view's post handler
    flow_step(flow, order=1, note="документированный шаг")(
        flow_urls.DocumentedView.post
    )

    index = endpoint_index()
    ref = (f"{flow_urls.DocumentedView.post.__module__}."
           f"{flow_urls.DocumentedView.post.__qualname__}")
    assert any(ep.path == "/api/documented/" for ep in index.get(ref, []))

    issues = check_flows()
    errors = [i.message for i in issues if i.level == "error"]
    # documented endpoint passes; the undocumented one is flagged
    assert not any("/api/documented/" in e for e in errors)
    assert any("/api/undocumented/" in e for e in errors)

    md = render_flow_markdown(flow, index)
    assert "POST `/api/documented/`" in md
