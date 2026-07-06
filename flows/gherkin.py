"""Flow → Gherkin (``.feature``) + playwright-bdd step-defs generator.

flow-system.md §3, wish #3: **the flow is the source, the ``.feature`` is a
projection** (not the other way round). A flow is already structured — ordered
``.human()`` / HTTP / comm steps with i18n-keyed notes and endpoint bindings —
so Gherkin is a deterministic view over it, not a second source of truth.

Two outputs per project language:

* ``render_feature`` — one ``Feature`` per flow, one happy-path ``Scenario``
  whose steps are the resolved (localized) step notes mapped to
  Given / When / Then / And by position (§7.20: the architect drafts a Gherkin
  scenario; here we derive it). Non-English languages emit the Gherkin
  ``# language:`` header and localized keywords, so the scenario reads in the
  project language (same i18n keys / catalogs as the SA-docs).

* ``render_step_defs`` — a `playwright-bdd <https://github.com/vitalets/
  playwright-bdd>`_ step library (runner pre-arbitrated for the workspace:
  TS-first, the codegen typed client in the step body, Playwright traces).
  **HTTP steps** drive the API through the generated ``StapelClient``
  (``@stapel/core``); **human / UI steps** are honest ``TODO(testid)`` stubs
  until a testid plan is attached to the flow (the flow model carries no
  testid yet — system-design §7.20); **action / function / task steps** assert
  a backend side-effect and are pending stubs. Nothing is invented.

Determinism: flows sort by id, steps by order, texts come from
``resolve_flow_texts`` — same registry + URLConf + catalogs ⇒ identical bytes,
which is what makes the release-gate drift check meaningful (flow-system.md
§4, the same discipline as the SA-doc trees).
"""
from __future__ import annotations

import re

from .registry import (
    STEP_ACTION,
    STEP_FUNCTION,
    STEP_HTTP,
    STEP_HUMAN,
    STEP_TASK,
    Flow,
)

# ---------------------------------------------------------------------------
# Gherkin localized keywords (subset of the official gherkin-languages.json).
# en is the default dialect — no ``# language:`` header; every other language
# emits the header plus its primary keyword forms so the parser (and a human)
# read the scenario in the project language.
# ---------------------------------------------------------------------------

GHERKIN: dict[str, dict[str, str | None]] = {
    "en": {
        "language": None,
        "feature": "Feature",
        "scenario": "Scenario",
        "given": "Given",
        "when": "When",
        "then": "Then",
        "and": "And",
        "actors": "Actors",
    },
    "ru": {
        "language": "ru",
        "feature": "Функция",
        "scenario": "Сценарий",
        "given": "Дано",
        "when": "Когда",
        "then": "Тогда",
        "and": "И",
        "actors": "Акторы",
    },
}


def gherkin_keywords(language: str | None) -> dict[str, str | None]:
    """Localized Gherkin keywords; unknown languages fall back to English."""
    return GHERKIN.get(language or "en", GHERKIN["en"])


def _base_keyword(index: int, total: int) -> str:
    """Positional BDD mapping of a step to a base keyword.

    The flow model does not classify steps as Given/When/Then, so we derive a
    deterministic, readable mapping: the first step is the precondition
    (``given``), the last is the outcome (``then``), everything between is an
    action (``when``). Consecutive steps sharing a base keyword render with
    the Gherkin ``And`` idiom (see :func:`render_feature`).
    """
    if total <= 1:
        return "then"
    if index == 0:
        return "given"
    if index == total - 1:
        return "then"
    return "when"


def _oneline(text: str) -> str:
    """Collapse whitespace to a single line (Gherkin step / description)."""
    return " ".join(text.split())


_CONVERTER_RE = re.compile(r"<[^:>]+:([^>]+)>")  # <str:challenge_id> → {challenge_id}
_BARE_RE = re.compile(r"<([^>]+)>")              # <challenge_id>      → {challenge_id}


def _openapi_path(path: str) -> str:
    """Django URL pattern → OpenAPI-style ``{name}`` path (for the step body)."""
    path = _CONVERTER_RE.sub(r"{\1}", path)
    return _BARE_RE.sub(r"{\1}", path)


# ---------------------------------------------------------------------------
# .feature rendering
# ---------------------------------------------------------------------------

def render_feature(
    flow: Flow,
    index: dict,
    texts: dict[str, str] | None = None,
    language: str | None = None,
) -> str:
    """Render one flow as a byte-stable ``.feature`` (see the module docstring).

    *texts* is the i18n key → text mapping from
    :func:`stapel_core.flows.i18n.resolve_flow_texts`; missing keys fall back
    to the in-code literals. *language* selects the Gherkin dialect and must
    match the language *texts* was resolved for.
    """
    _t = (texts or {}).get
    g = gherkin_keywords(language)
    lines: list[str] = []
    if g["language"]:
        lines.append(f"# language: {g['language']}")
    lines.append(
        f"# Generated from flow {flow.id} by generate_flow_features — do not "
        f"edit; regenerate (flow-system.md §3)."
    )
    lines.append(f"@flow:{flow.id}")
    lines.append(f"{g['feature']}: {_oneline(_t(flow.title_key, flow.title))}")
    lines.append("")

    description = _oneline(_t(flow.description_key, flow.description))
    if description:
        lines.append(f"  {description}")
        lines.append("")
    if flow.actors:
        lines.append(f"  # {g['actors']}: " + ", ".join(flow.actors))

    lines.append(f"  {g['scenario']}: {_oneline(_t(flow.title_key, flow.title))}")

    steps = flow.sorted_steps()
    total = len(steps)
    prev_base: str | None = None
    for i, step in enumerate(steps):
        base = _base_keyword(i, total)
        keyword = g["and"] if base == prev_base else g[base]
        prev_base = base
        note = _oneline(_t(step.note_key, step.note))
        lines.append(f"    {keyword} {note}")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# playwright-bdd step definitions
# ---------------------------------------------------------------------------

_JS_RE_META = re.compile(r"([.*+?^${}()|\[\]\\/])")

#: base keyword → playwright-bdd registration function.
_STEP_FN = {"given": "Given", "when": "When", "then": "Then"}


def _escape_js_regex(text: str) -> str:
    """Escape *text* for use inside a JS ``/.../`` regex literal."""
    return _JS_RE_META.sub(r"\\\1", text)


def _js_string(text: str) -> str:
    """A double-quoted JS string literal for *text*."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _http_body(flow: Flow, step, index: dict) -> list[str]:
    """Step body for an HTTP step: drive the codegen typed client."""
    eps = index.get(step.ref, [])
    if not eps:
        return [
            f"    // TODO: endpoint for `{step.ref}` not found in the URLConf.",
            f"    throw new Error(\"pending endpoint: {flow.id} step {step.order}\");",
        ]
    ep = eps[0]
    path = _openapi_path(ep.path)
    if "{" in path:
        return [
            f"    // TODO: fill the path parameter(s) of {ep.method} {path} "
            f"from a prior step.",
            f"    throw new Error(\"pending parametrized request: {flow.id} "
            f"step {step.order}\");",
        ]
    return [
        f"    stapel.response = await stapel.client.request({_js_string(path)}, "
        f"{{ method: {_js_string(ep.method)} }});",
    ]


def _human_body(flow: Flow, step) -> list[str]:
    return [
        f"    // TODO(testid): UI step — attach a testid plan to flow "
        f"{flow.id} step {step.order} (system-design §7.20).",
        f"    throw new Error(\"pending UI step: {flow.id} step {step.order}\");",
    ]


def _effect_body(flow: Flow, step) -> list[str]:
    kind = {STEP_ACTION: "action", STEP_FUNCTION: "function",
            STEP_TASK: "task"}[step.kind]
    return [
        f"    // TODO: assert the {kind} side-effect `{step.ref}` of flow "
        f"{flow.id}.",
        f"    throw new Error(\"pending effect assertion: {flow.id} "
        f"{step.ref}\");",
    ]


def render_step_defs(
    flows: list[Flow],
    index: dict,
    texts: dict[str, str] | None = None,
    language: str | None = None,
) -> str:
    """Render the playwright-bdd step library matching the ``.feature`` set.

    The regex of each step definition is the *resolved* note (the same text
    the ``.feature`` step carries), so a single-language bundle
    (``.feature`` + steps) is self-consistent. HTTP steps call the codegen
    typed client; human and comm steps are honest pending stubs.
    """
    _t = (texts or {}).get
    lines = [
        f"// Generated by generate_flow_features (language: {language or 'en'}) "
        f"— do not edit.",
        "// flow-system.md §3: the flow is the source, these step-defs are a "
        "projection.",
        "// HTTP steps drive the codegen typed client (@stapel/core "
        "StapelClient); human/UI",
        "// steps are TODO(testid) stubs (no testid plan on the flow yet, "
        "system-design §7.20);",
        "// action/function/task steps assert a backend side-effect and are "
        "pending.",
        'import { createBdd } from "playwright-bdd";',
        'import { test } from "./fixtures";',
        "",
        "const { Given, When, Then } = createBdd(test);",
        "",
    ]

    for flow in flows:
        steps = flow.sorted_steps()
        total = len(steps)
        for i, step in enumerate(steps):
            base = _base_keyword(i, total)
            fn = _STEP_FN[base]
            note = _oneline(_t(step.note_key, step.note))
            pattern = _escape_js_regex(note)
            if step.kind == STEP_HTTP:
                eps = index.get(step.ref, [])
                where = (f" · {eps[0].method} {eps[0].path}" if eps else "")
                body = _http_body(flow, step, index)
            elif step.kind == STEP_HUMAN:
                where = ""
                body = _human_body(flow, step)
            else:
                where = f" · {step.ref}"
                body = _effect_body(flow, step)
            lines.append(
                f"// {flow.id} — step {step.order} ({step.kind}{where})"
            )
            lines.append(f"{fn}(/^{pattern}$/, async ({{ stapel }}) => {{")
            lines.extend(body)
            lines.append("});")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_fixtures(language: str | None = None) -> str:
    """Render the playwright-bdd fixtures scaffold for a language bundle.

    Provides the ``stapel`` world (the codegen ``StapelClient`` + the last
    API response) the generated step-defs consume. Byte-stable; the project
    wires ``baseUrl`` at the system under test.
    """
    return "\n".join([
        f"// Generated by generate_flow_features (language: {language or 'en'}) "
        f"— do not edit.",
        "// Fixtures scaffold for the flow BDD suite: the `stapel` world holds "
        "the codegen",
        "// typed client (@stapel/core) and the last API response, shared "
        "across steps.",
        'import { test as base } from "playwright-bdd";',
        'import { createStapelClient, type StapelClient } from "@stapel/core";',
        "",
        "export interface StapelWorld {",
        "  client: StapelClient;",
        "  response: unknown;",
        "}",
        "",
        "export const test = base.extend<{ stapel: StapelWorld }>({",
        "  stapel: async ({}, use) => {",
        "    // TODO: point baseUrl at the system under test.",
        "    const client = createStapelClient({",
        '      baseUrl: process.env.STAPEL_API_BASE ?? "/api",',
        "    });",
        "    await use({ client, response: undefined });",
        "  },",
        "});",
        "",
    ])


__all__ = [
    "GHERKIN",
    "gherkin_keywords",
    "render_feature",
    "render_fixtures",
    "render_step_defs",
]
