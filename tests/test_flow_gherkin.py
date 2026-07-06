"""Gherkin projection (flow-system.md §3): .feature rendering, localized
dialects, playwright-bdd step-defs over the codegen typed client, the
``generate_flow_features`` bundles and their byte-stability."""
import io

import pytest
from django.core.management import call_command
from django.test import override_settings
from rest_framework import serializers
from rest_framework.views import APIView

from stapel_core.flows import (
    Flow,
    flow_registry,
    flow_step,
    render_feature,
    render_fixtures,
    render_step_defs,
)
from stapel_core.flows.conf import flows_settings
from stapel_core.flows.docs import endpoint_index
from stapel_core.flows.gherkin import (
    _base_keyword,
    _escape_js_regex,
    _openapi_path,
    gherkin_keywords,
)


@pytest.fixture(autouse=True)
def clean_registry():
    flow_registry.clear()
    flows_settings.reload()
    yield
    flow_registry.clear()
    flows_settings.reload()


def _flow(flow_id="test.gherkin", **kw):
    d = dict(
        title="Gherkin scenario",
        description="A scenario description long enough to clear the "
                    "completeness threshold used by check_flows.",
        actors=["Anonymous user"],
    )
    d.update(kw)
    return Flow(flow_id, **d)


class _ReqSer(serializers.Serializer):
    pass


class _LoginView(APIView):
    request_serializer_class = _ReqSer

    def post(self, request):  # pragma: no cover - never called
        pass


class _ChallengeView(APIView):
    def get(self, request, challenge_id):  # pragma: no cover - never called
        pass


def _urls():
    from django.urls import path
    return [
        path("api/login/", _LoginView.as_view()),
        path("api/verification/<str:challenge_id>/", _ChallengeView.as_view()),
    ]


urlpatterns = _urls()


# ---------------------------------------------------------------------------
# Keyword mapping / helpers
# ---------------------------------------------------------------------------

def test_positional_keyword_mapping():
    # first = given, last = then, middle = when; single step = then
    assert _base_keyword(0, 1) == "then"
    assert _base_keyword(0, 3) == "given"
    assert _base_keyword(1, 3) == "when"
    assert _base_keyword(2, 3) == "then"


def test_gherkin_keywords_localize_and_fall_back():
    assert gherkin_keywords("ru")["given"] == "Дано"
    assert gherkin_keywords("ru")["language"] == "ru"
    assert gherkin_keywords("en")["language"] is None
    # unknown language falls back to English keywords
    assert gherkin_keywords("de")["given"] == "Given"
    assert gherkin_keywords(None)["feature"] == "Feature"


def test_openapi_path_converts_django_converters():
    assert _openapi_path("/api/verification/<str:challenge_id>/") == \
        "/api/verification/{challenge_id}/"
    assert _openapi_path("/x/<pk>/") == "/x/{pk}/"


def test_escape_js_regex_neutralizes_metachars():
    escaped = _escape_js_regex("429 on rate limit (30s); code {x} / a+b?")
    assert "\\(" in escaped and "\\)" in escaped
    assert "\\{" in escaped and "\\}" in escaped
    assert "\\+" in escaped and "\\?" in escaped and "\\/" in escaped


# ---------------------------------------------------------------------------
# .feature rendering
# ---------------------------------------------------------------------------

@override_settings(ROOT_URLCONF="tests.test_flow_gherkin")
def test_feature_renders_scenario_with_given_when_then():
    flow = _flow()
    flow.human(order=0, note="The user enters their email")
    flow_step(flow, order=1, note="Request a one-time code")(_LoginView.post)
    flow.action("user.registered", order=2, note="Emitted on first login")
    feature = render_feature(flow, endpoint_index())
    assert "Feature: Gherkin scenario" in feature
    assert "@flow:test.gherkin" in feature
    assert "  Scenario: Gherkin scenario" in feature
    assert "    Given The user enters their email" in feature
    assert "    When Request a one-time code" in feature
    assert "    Then Emitted on first login" in feature
    # en is the default dialect: no language header
    assert "# language:" not in feature
    assert feature.endswith("\n") and not feature.endswith("\n\n")


def test_feature_consecutive_same_keyword_becomes_and():
    flow = _flow("test.and")
    flow.human(order=0, note="step a")
    flow.human(order=1, note="step b")
    flow.human(order=2, note="step c")
    flow.human(order=3, note="step d")
    feature = render_feature(flow, {})
    assert "    Given step a" in feature
    assert "    When step b" in feature
    assert "    And step c" in feature   # second consecutive when
    assert "    Then step d" in feature


def test_feature_localizes_to_ru_with_language_header():
    flow = _flow("test.ru")
    flow.human(order=0, note="User enters email")
    flow.human(order=1, note="User waits")
    flow.human(order=2, note="Session appears")
    texts = {
        "flow.test.ru.title": "Вход по коду",
        "flow.test.ru.description": "Описание сценария.",
        "flow.test.ru.step.0.note": "Пользователь вводит email",
        "flow.test.ru.step.1.note": "Пользователь ждёт",
        "flow.test.ru.step.2.note": "Появляется сессия",
    }
    feature = render_feature(flow, {}, texts=texts, language="ru")
    assert feature.startswith("# language: ru\n")
    assert "Функция: Вход по коду" in feature
    assert "  Сценарий: Вход по коду" in feature
    assert "    Дано Пользователь вводит email" in feature
    assert "    Когда Пользователь ждёт" in feature
    assert "    Тогда Появляется сессия" in feature


def test_feature_notes_are_single_line():
    flow = _flow("test.multiline")
    flow.human(order=0, note="a note\nspread over\n   lines")
    feature = render_feature(flow, {})
    assert "a note spread over lines" in feature


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------

@override_settings(ROOT_URLCONF="tests.test_flow_gherkin")
def test_step_defs_http_step_drives_typed_client():
    flow = _flow()
    flow.human(order=0, note="The user enters their email")
    flow_step(flow, order=1, note="Request a one-time code")(_LoginView.post)
    flow.action("user.registered", order=2, note="Emitted on first login")
    steps = render_step_defs([flow], endpoint_index())
    assert 'import { createBdd } from "playwright-bdd";' in steps
    assert "const { Given, When, Then } = createBdd(test);" in steps
    # HTTP step: real client call with the resolved method+path
    assert 'stapel.client.request("/api/login/", { method: "POST" })' in steps
    # step regex is the note, registered under the positional keyword
    assert "When(/^Request a one\\-time code$/" not in steps  # '-' not escaped
    assert "When(/^Request a one-time code$/" in steps
    assert "Given(/^The user enters their email$/" in steps
    assert "Then(/^Emitted on first login$/" in steps
    # human + action steps are honest pending stubs
    assert "TODO(testid)" in steps
    assert "pending effect assertion: test.gherkin user.registered" in steps


@override_settings(ROOT_URLCONF="tests.test_flow_gherkin")
def test_step_defs_parametrized_path_is_pending_todo():
    flow = _flow("test.params")
    flow.human(order=0, note="Client got a challenge")
    flow_step(flow, order=1, note="Read the challenge")(_ChallengeView.get)
    flow.human(order=2, note="Done")
    steps = render_step_defs([flow], endpoint_index())
    # path params cannot be invented — honest TODO with the OpenAPI-form path
    assert "GET /api/verification/{challenge_id}/" in steps
    assert "pending parametrized request: test.params step 1" in steps


def test_step_defs_missing_endpoint_is_pending_todo():
    flow = _flow("test.lost")

    @flow_step(flow, order=1, note="Calls an unrouted view")
    def post(self, request):  # pragma: no cover - never called
        pass

    steps = render_step_defs([flow], {})
    assert "not found in the URLConf" in steps
    assert "pending endpoint: test.lost step 1" in steps


def test_step_defs_use_resolved_texts_for_regexes():
    flow = _flow("test.i18n")
    flow.human(order=0, note="User enters email")
    flow.human(order=1, note="Done")
    texts = {
        "flow.test.i18n.step.0.note": "Пользователь вводит email",
        "flow.test.i18n.step.1.note": "Готово",
    }
    steps = render_step_defs([flow], {}, texts=texts, language="ru")
    assert "Given(/^Пользователь вводит email$/" in steps
    assert "Then(/^Готово$/" in steps


def test_fixtures_scaffold_exports_stapel_world():
    fixtures = render_fixtures("en")
    assert 'import { createStapelClient, type StapelClient } from "@stapel/core";' \
        in fixtures
    assert "export const test = base.extend<{ stapel: StapelWorld }>" in fixtures


# ---------------------------------------------------------------------------
# generate_flow_features — bundles, byte-stable
# ---------------------------------------------------------------------------

@override_settings(ROOT_URLCONF="tests.test_flow_gherkin")
def test_generate_flow_features_writes_bilingual_byte_stable_bundles(tmp_path):
    flow = _flow("test.bundle")
    flow.human(order=0, note="The user enters their email")
    flow_step(flow, order=1, note="Request a one-time code")(_LoginView.post)
    flow.action("user.registered", order=2, note="Emitted on first login")
    out = tmp_path / "features"

    from stapel_core.django.management.commands.generate_flow_features import (
        Command,
    )

    def _run():
        call_command(Command(), "--out", str(out),
                     "--languages", "en,ru", stdout=io.StringIO())

    _run()
    for lang in ("en", "ru"):
        assert (out / lang / "test.bundle.feature").is_file()
        assert (out / lang / "steps" / "flows.steps.ts").is_file()
        assert (out / lang / "steps" / "fixtures.ts").is_file()
    root_readme = (out / "README.md").read_text()
    assert "[English](en/)" in root_readme and "[Русский](ru/)" in root_readme
    # en bundle carries English keywords, ru bundle the ru dialect header
    assert "Feature:" in (out / "en" / "test.bundle.feature").read_text()
    assert (out / "ru" / "test.bundle.feature").read_text().startswith(
        "# language: ru\n")

    # byte-stable: a second run over the same registry reproduces every byte
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    _run()
    after = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert before == after


def test_generate_flow_features_no_flows_warns(tmp_path):
    from stapel_core.django.management.commands.generate_flow_features import (
        Command,
    )

    buf = io.StringIO()
    call_command(Command(), "--out", str(tmp_path / "f"), stdout=buf)
    assert "no flows registered" in buf.getvalue()
