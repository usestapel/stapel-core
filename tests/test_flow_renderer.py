"""SA-document renderer (flow-system.md §4): mermaid diagram, endpoint
tables, verification contracts, localized chrome, the FLOW_DOC_RENDERER
seam and the bilingual ``generate_project_docs`` trees."""
import io
import json

import pytest
from django.core.management import call_command
from django.test import override_settings
from rest_framework import serializers, viewsets
from rest_framework.views import APIView

from stapel_core.flows import (
    Flow,
    flow_registry,
    flow_step,
    get_flow_doc_renderer,
    render_flow_markdown,
    render_index_markdown,
)
from stapel_core.flows.conf import flows_settings
from stapel_core.flows.docs import (
    DefaultFlowDocRenderer,
    chrome,
    endpoint_index,
)
from stapel_core.verification.decorators import requires_verification


@pytest.fixture(autouse=True)
def clean_registry():
    flow_registry.clear()
    flows_settings.reload()
    yield
    flow_registry.clear()
    flows_settings.reload()


def _flow(flow_id="test.render", **kw):
    d = dict(
        title="Rendered scenario",
        description="A scenario description long enough to clear the "
                    "completeness threshold used by check_flows.",
        actors=["Anonymous user"],
    )
    d.update(kw)
    return Flow(flow_id, **d)


# ---------------------------------------------------------------------------
# Mermaid diagram
# ---------------------------------------------------------------------------

def test_mermaid_diagram_is_github_native_and_sequential():
    flow = _flow()
    flow.human(order=0, note="User enters email")
    flow.action("user.registered", order=2, note='Emitted with "quotes" inside')
    md = render_flow_markdown(flow, {})
    assert "```mermaid\nflowchart TD" in md
    # human = stadium, action = subroutine node shapes
    assert 's1(["1. User action"])' in md
    assert 's2[["2. Action: user.registered"]]' in md
    # sequential edges between consecutive steps
    assert "s1 --> s2" in md
    # the fenced block is closed
    assert md.count("```") == 2


def test_mermaid_labels_are_sanitized():
    # A flow whose *short* label could carry quotes must not break mermaid.
    flow = _flow("test.quotes")
    flow.human(order=0, note="whatever")
    diagram = DefaultFlowDocRenderer()._diagram(flow, {}, chrome("en"))
    text = "\n".join(diagram)
    # node label is quoted and single-line; no stray double quotes inside
    assert '(["1. User action"])' in text


def test_mermaid_neutralizes_django_path_converters():
    # GitHub renders mermaid labels as HTML — <str:challenge_id> would be
    # swallowed as an unknown tag, so it must become the {name} form.
    from stapel_core.flows.docs import _mermaid_label

    assert _mermaid_label("GET /verification/<str:challenge_id>/") == \
        "GET /verification/{challenge_id}/"
    assert _mermaid_label("<slug:slug>/login") == "{slug}/login"
    assert "<" not in _mermaid_label("a <weird> b") and \
        ">" not in _mermaid_label("a <weird> b")


def test_no_diagram_for_a_stepless_flow():
    flow = _flow("test.empty")
    assert "```mermaid" not in render_flow_markdown(flow, {})


# ---------------------------------------------------------------------------
# Endpoints table + verification contract
# ---------------------------------------------------------------------------

class _ReqSer(serializers.Serializer):
    pass


class _RespSer(serializers.Serializer):
    pass


class _ProtectedView(APIView):
    request_serializer_class = _ReqSer
    response_serializer_class = _RespSer

    @requires_verification(scope="payout", factors=["otp_email", "totp"])
    def post(self, request):  # pragma: no cover - never called
        pass


_URLPATTERNS = None


def _protected_urls():
    from django.urls import path
    return [path("api/payout/", _ProtectedView.as_view())]


@override_settings(ROOT_URLCONF="tests.test_flow_renderer")
def test_endpoints_table_lists_serializers_and_verification():
    flow = _flow("test.protected")
    flow_step(flow, order=1, note="Trigger the payout; step-up required")(
        _ProtectedView.post
    )
    index = endpoint_index()
    md = render_flow_markdown(flow, index)
    assert "## Endpoints" in md
    assert "| Step | Method | Path | Request | Response | Step-up verification |" in md
    assert "_ReqSer" in md and "_RespSer" in md
    assert "`payout` (otp_email, totp)" in md


class _DemoViewSet(viewsets.ViewSet):
    def list(self, request):  # pragma: no cover - never called
        pass


def _viewset_urls():
    from django.urls import path
    # DRF binds an auto ``head`` mirroring ``get`` at request time; declaring
    # it up-front here simulates a ViewSet that has already been hit.
    return [path("api/things/",
                 _DemoViewSet.as_view({"get": "list", "head": "list"}))]


@override_settings(ROOT_URLCONF="tests._viewset_urlconf")
def test_iter_endpoints_skips_framework_auto_head(monkeypatch):
    # Build a throwaway URLConf module holding the ViewSet route.
    import sys
    import types

    mod = types.ModuleType("tests._viewset_urlconf")
    mod.urlpatterns = _viewset_urls()
    monkeypatch.setitem(sys.modules, "tests._viewset_urlconf", mod)

    from stapel_core.flows.docs import iter_api_endpoints

    methods = {ep.method for ep in iter_api_endpoints()
               if ep.path == "/api/things/"}
    assert "GET" in methods
    assert "HEAD" not in methods and "OPTIONS" not in methods


# module-level urlpatterns for the override_settings ROOT_URLCONF above
urlpatterns = _protected_urls()


# ---------------------------------------------------------------------------
# Localized chrome
# ---------------------------------------------------------------------------

def test_chrome_defaults_to_english_and_localizes_ru():
    flow = _flow()
    flow.human(order=0, note="n")
    en = render_flow_markdown(flow, {})
    ru = render_flow_markdown(flow, {}, language="ru")
    assert "**Actors:**" in en and "## Steps" in en and "## Flow diagram" in en
    assert "**Актор(ы):**" in ru and "## Шаги" in ru and "## Диаграмма флоу" in ru
    # unknown language falls back to English chrome
    de = render_flow_markdown(flow, {}, language="de")
    assert "## Steps" in de


def test_index_chrome_localizes():
    flow = _flow()
    flow.human(order=0, note="n")
    en = render_index_markdown([flow], {})
    ru = render_index_markdown([flow], {}, language="ru")
    assert "# Flows" in en and "Endpoint → flow" in en
    assert "# Флоу" in ru and "Эндпоинт → флоу" in ru


# ---------------------------------------------------------------------------
# FLOW_DOC_RENDERER seam
# ---------------------------------------------------------------------------

class _CustomRenderer:
    def render_flow(self, flow, index, texts=None, language=None):
        return f"CUSTOM {flow.id}\n"

    def render_index(self, flows, index, texts=None, language=None):
        return "CUSTOM INDEX\n"


def test_flow_doc_renderer_seam_is_swappable():
    assert isinstance(get_flow_doc_renderer(), DefaultFlowDocRenderer)
    with override_settings(
        STAPEL_FLOWS={"FLOW_DOC_RENDERER": "tests.test_flow_renderer._CustomRenderer"}
    ):
        flows_settings.reload()
        renderer = get_flow_doc_renderer()
        assert isinstance(renderer, _CustomRenderer)
        assert renderer.render_flow(_flow(), {}) == "CUSTOM test.render\n"


# ---------------------------------------------------------------------------
# generate_project_docs — bilingual trees, byte-stable
# ---------------------------------------------------------------------------

@override_settings(ROOT_URLCONF="tests.test_flow_renderer")
def test_generate_project_docs_writes_bilingual_byte_stable_trees(tmp_path, monkeypatch):
    # A registered flow the command can discover (autodiscover is a no-op
    # here — the registry already holds this one).
    flow = _flow("test.project")
    flow_step(flow, order=1, note="Trigger the payout; step-up required")(
        _ProtectedView.post
    )
    out = tmp_path / "flows"

    # The command instance is invoked directly: its owning app
    # (common_django) is not in the core test INSTALLED_APPS, so name-based
    # discovery would miss it — passing the instance bypasses that.
    from stapel_core.django.management.commands.generate_project_docs import Command

    def _run():
        call_command(Command(), "--out", str(out),
                     "--languages", "en,ru", stdout=io.StringIO())

    _run()
    # language-agnostic artifact once at the root
    assert (out / "flows.json").is_file()
    data = json.loads((out / "flows.json").read_text())
    assert data[0]["id"] == "test.project"
    # one tree per language, each with the flow doc + index
    for lang in ("en", "ru"):
        assert (out / lang / "test.project.md").is_file()
        assert (out / lang / "README.md").is_file()
    # top-level index links both trees
    root_readme = (out / "README.md").read_text()
    assert "en/README.md" in root_readme and "ru/README.md" in root_readme
    # chrome differs per language
    assert "## Steps" in (out / "en" / "test.project.md").read_text()
    assert "## Шаги" in (out / "ru" / "test.project.md").read_text()

    # byte-stable: a second run over the same registry reproduces every byte
    before = {p.name: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    _run()
    after = {p.name: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert before == after
