"""Flow → markdown SA-documentation renderer.

Resolves HTTP steps against the live URLConf: method + path, serializer
classes (via the view's seam attributes when present), permissions and the
step-up verification contract (x-stapel-verification attribute). Non-HTTP
steps render from the comm registries/schemas.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .registry import (
    FLOWS_ATTR,
    STEP_ACTION,
    STEP_FUNCTION,
    STEP_HTTP,
    STEP_HUMAN,
    STEP_TASK,
    Flow,
)

logger = logging.getLogger(__name__)


@dataclass
class EndpointInfo:
    method: str
    path: str
    view_ref: str
    view_cls: type | None
    # Name of the handler attribute on the class: the http verb for
    # APIViews, the action name for ViewSets (e.g. "list", custom actions).
    handler_name: str = ""


def iter_api_endpoints() -> list[EndpointInfo]:
    """Walk the URLConf and return every API endpoint with its view ref."""
    from django.conf import settings
    from django.urls import get_resolver

    if not getattr(settings, "ROOT_URLCONF", None):
        return []

    endpoints: list[EndpointInfo] = []

    def _walk(patterns, prefix: str):
        for p in patterns:
            if hasattr(p, "url_patterns"):  # resolver (include)
                _walk(p.url_patterns, prefix + str(p.pattern))
                continue
            callback = p.callback
            cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
            actions = getattr(callback, "actions", None)  # ViewSet mapping
            path = "/" + (prefix + str(p.pattern)).lstrip("/")
            if actions:
                for http_method, action in actions.items():
                    # DRF mutates a ViewSet's ``actions`` at *request* time,
                    # binding an auto ``head`` (mirroring ``get``); reading it
                    # here would make the docs depend on whether the endpoint
                    # was hit at runtime. Skip framework-auto verbs — HEAD /
                    # OPTIONS are never business steps — for a byte-stable
                    # render and stable endpoint-coverage checks.
                    if http_method.lower() in ("head", "options"):
                        continue
                    handler = getattr(cls, action, None)
                    ref = _callable_ref(handler, cls, action)
                    endpoints.append(
                        EndpointInfo(http_method.upper(), path, ref, cls, action)
                    )
            elif cls is not None:
                for http_method in ("get", "post", "put", "patch", "delete"):
                    handler = getattr(cls, http_method, None)
                    if handler is None:
                        continue
                    ref = _callable_ref(handler, cls, http_method)
                    endpoints.append(
                        EndpointInfo(http_method.upper(), path, ref, cls, http_method)
                    )
            else:
                ref = f"{callback.__module__}.{callback.__qualname__}"
                endpoints.append(EndpointInfo("*", path, ref, None))

    _walk(get_resolver().url_patterns, "")
    return endpoints


def _callable_ref(handler, cls, method_name: str) -> str:
    if handler is not None:
        return f"{handler.__module__}.{handler.__qualname__}"
    return f"{cls.__module__}.{cls.__qualname__}.{method_name}"


def _flow_refs(view_cls, handler) -> set[str]:
    """Flow ids attached to the handler or its class."""
    ids: set[str] = set()
    for target in (handler, view_cls):
        for m in getattr(target, FLOWS_ATTR, []) or []:
            ids.add(m["flow"])
    return ids


def endpoint_index() -> dict[str, list[EndpointInfo]]:
    """view ref → endpoints (an attached step may map to several routes)."""
    index: dict[str, list[EndpointInfo]] = {}
    for ep in iter_api_endpoints():
        index.setdefault(ep.view_ref, []).append(ep)
        # class-level attachment: also index by the class ref
        if ep.view_cls is not None:
            cls_ref = f"{ep.view_cls.__module__}.{ep.view_cls.__qualname__}"
            index.setdefault(cls_ref, []).append(ep)
    return index


def _serializer_names(view_cls) -> dict[str, str]:
    out = {}
    for attr in ("request_serializer_class", "response_serializer_class",
                 "serializer_class"):
        val = getattr(view_cls, attr, None)
        if val is not None:
            out[attr] = getattr(val, "__name__", str(val))
    return out


# ---------------------------------------------------------------------------
# Renderer chrome (flow-system.md §4)
#
# The *content* (title/description/notes) is resolved from i18n keys; the
# renderer's own scaffolding words (headings, table columns, "User action")
# are localized here. Unknown languages fall back to English chrome, so a
# module that ships only en/ru catalogs still renders any DOC language with
# English scaffolding around translated content.
# ---------------------------------------------------------------------------

CHROME: dict[str, dict[str, str]] = {
    "en": {
        "actors": "Actors",
        "diagram": "Flow diagram",
        "steps": "Steps",
        "endpoints": "Endpoints",
        "user_action": "User action",
        "not_found": "endpoint not found in URLConf",
        "col_step": "Step",
        "col_method": "Method",
        "col_path": "Path",
        "col_request": "Request",
        "col_response": "Response",
        "col_verification": "Step-up verification",
        "none": "—",
        "verification_defaults": "(defaults)",
        "index_title": "Flows",
        "col_id": "ID",
        "col_name": "Name",
        "col_steps": "Steps",
        "endpoint_to_flow": "Endpoint → flow",
        "kind_action": "Action",
        "kind_function": "Function",
        "kind_task": "Task",
    },
    "ru": {
        "actors": "Актор(ы)",
        "diagram": "Диаграмма флоу",
        "steps": "Шаги",
        "endpoints": "Эндпоинты",
        "user_action": "Действие пользователя",
        "not_found": "эндпоинт не найден в URLConf",
        "col_step": "Шаг",
        "col_method": "Метод",
        "col_path": "Путь",
        "col_request": "Запрос",
        "col_response": "Ответ",
        "col_verification": "Step-up-верификация",
        "none": "—",
        "verification_defaults": "(по умолчанию)",
        "index_title": "Флоу",
        "col_id": "ID",
        "col_name": "Название",
        "col_steps": "Шагов",
        "endpoint_to_flow": "Эндпоинт → флоу",
        "kind_action": "Action",
        "kind_function": "Function",
        "kind_task": "Task",
    },
}


def chrome(language: str | None) -> dict[str, str]:
    """Localized renderer scaffolding words; unknown languages → English."""
    return CHROME.get(language or "en", CHROME["en"])


def _mermaid_label(text: str) -> str:
    """A mermaid-safe, single-line node label (quoted at the call site).

    GitHub renders mermaid flowchart labels as HTML, so Django path
    converters (``<str:challenge_id>``) would be swallowed as an unknown
    tag. Convert them to the OpenAPI ``{name}`` form and neutralize any
    stray angle brackets; also fold whitespace and drop double quotes.
    """
    text = " ".join(text.split()).replace('"', "'")
    text = re.sub(r"<[^:>]+:([^>]+)>", r"{\1}", text)  # <str:challenge_id>
    text = re.sub(r"<([^>]+)>", r"{\1}", text)         # <challenge_id>
    return text.replace("<", "(").replace(">", ")")


class DefaultFlowDocRenderer:
    """Default FLOW_DOC_RENDERER: a byte-stable markdown SA-document.

    Per flow: a GitHub-native mermaid step diagram, the numbered steps, and
    an endpoints table (serializers + the step-up verification contract).
    Deterministic throughout — same registry + URLConf ⇒ identical bytes,
    which is what makes the release-gate drift check meaningful.
    """

    # --- one flow --------------------------------------------------------

    def render_flow(
        self,
        flow: Flow,
        index: dict[str, list[EndpointInfo]],
        texts: dict[str, str] | None = None,
        language: str | None = None,
    ) -> str:
        _t = (texts or {}).get
        c = chrome(language)
        lines = [f"# {_t(flow.title_key, flow.title)}", "", f"`{flow.id}`", ""]
        if flow.actors:
            lines += [f"**{c['actors']}:** " + ", ".join(flow.actors), ""]
        lines += [_t(flow.description_key, flow.description).strip(), ""]

        lines += self._diagram(flow, index, c)
        lines += self._steps(flow, index, _t, c)
        lines += self._endpoints_table(flow, index, c)
        return "\n".join(lines).rstrip() + "\n"

    def _diagram(self, flow: Flow, index, c: dict[str, str]) -> list[str]:
        steps = flow.sorted_steps()
        if not steps:
            return []
        lines = [f"## {c['diagram']}", "", "```mermaid", "flowchart TD"]
        node_ids: list[str] = []
        for i, step in enumerate(steps, 1):
            node_id = f"s{i}"
            node_ids.append(node_id)
            label = _mermaid_label(f"{i}. {self._short(step, index, c)}")
            if step.kind == STEP_HUMAN:
                lines.append(f'    {node_id}(["{label}"])')
            elif step.kind == STEP_HTTP:
                lines.append(f'    {node_id}["{label}"]')
            else:  # action / function / task
                lines.append(f'    {node_id}[["{label}"]]')
        for a, b in zip(node_ids, node_ids[1:]):
            lines.append(f"    {a} --> {b}")
        lines += ["```", ""]
        return lines

    def _short(self, step, index, c: dict[str, str]) -> str:
        """A concise, diagram-safe label for one step (no free-form note)."""
        if step.kind == STEP_HTTP:
            eps = index.get(step.ref, [])
            if eps:
                ep = eps[0]
                return f"{ep.method} {ep.path}"
            return step.ref.rsplit(".", 1)[-1]
        if step.kind == STEP_HUMAN:
            return c["user_action"]
        kind_label = {STEP_ACTION: c["kind_action"], STEP_FUNCTION: c["kind_function"],
                      STEP_TASK: c["kind_task"]}[step.kind]
        return f"{kind_label}: {step.ref}"

    def _steps(self, flow: Flow, index, _t, c: dict[str, str]) -> list[str]:
        lines = [f"## {c['steps']}", ""]
        for i, step in enumerate(flow.sorted_steps(), 1):
            note = _t(step.note_key, step.note)
            if step.kind == STEP_HTTP:
                eps = index.get(step.ref, [])
                if eps:
                    ep = eps[0]
                    lines.append(f"{i}. **{ep.method} `{ep.path}`** — {note}")
                else:
                    lines.append(f"{i}. **HTTP** `{step.ref}` — {note} "
                                 f"_({c['not_found']})_")
            elif step.kind in (STEP_ACTION, STEP_FUNCTION, STEP_TASK):
                kind_label = {STEP_ACTION: c["kind_action"],
                              STEP_FUNCTION: c["kind_function"],
                              STEP_TASK: c["kind_task"]}[step.kind]
                lines.append(f"{i}. **{kind_label} `{step.ref}`** — {note}")
            elif step.kind == STEP_HUMAN:
                lines.append(f"{i}. **{c['user_action']}** — {note}")
        lines.append("")
        return lines

    def _endpoints_table(self, flow: Flow, index, c: dict[str, str]) -> list[str]:
        from stapel_core.verification.decorators import view_verification_contract

        rows: list[str] = []
        for i, step in enumerate(flow.sorted_steps(), 1):
            if step.kind != STEP_HTTP:
                continue
            for ep in index.get(step.ref, []):
                request = response = c["none"]
                verification = c["none"]
                if ep.view_cls is not None:
                    sers = _serializer_names(ep.view_cls)
                    request = sers.get("request_serializer_class") or c["none"]
                    response = (sers.get("response_serializer_class")
                                or sers.get("serializer_class") or c["none"])
                    contract = view_verification_contract(ep.view_cls)
                    if contract:
                        factors = contract.get("factors") or [c["verification_defaults"]]
                        verification = (f"`{contract['scope']}` "
                                        f"({', '.join(factors)})")
                rows.append(
                    f"| {i} | {ep.method} | `{ep.path}` | {request} | "
                    f"{response} | {verification} |"
                )
        if not rows:
            return []
        header = (f"## {c['endpoints']}", "",
                  f"| {c['col_step']} | {c['col_method']} | {c['col_path']} | "
                  f"{c['col_request']} | {c['col_response']} | "
                  f"{c['col_verification']} |",
                  "|---|---|---|---|---|---|")
        return [*header, *rows, ""]

    # --- index -----------------------------------------------------------

    def render_index(
        self,
        flows: list[Flow],
        index,
        texts: dict[str, str] | None = None,
        language: str | None = None,
    ) -> str:
        _t = (texts or {}).get
        c = chrome(language)
        lines = [f"# {c['index_title']}", "",
                 f"| {c['col_id']} | {c['col_name']} | {c['col_steps']} |",
                 "|---|---|---|"]
        for f in flows:
            lines.append(
                f"| [`{f.id}`]({f.id}.md) | {_t(f.title_key, f.title)} | "
                f"{len(f.steps)} |"
            )
        lines += ["", f"## {c['endpoint_to_flow']}", ""]
        reverse: dict[str, set[str]] = {}
        for f in flows:
            for step in f.steps:
                if step.kind == STEP_HTTP:
                    for ep in index.get(step.ref, []):
                        reverse.setdefault(f"{ep.method} {ep.path}", set()).add(f.id)
        for ep_key in sorted(reverse):
            lines.append(f"- `{ep_key}` → " + ", ".join(sorted(reverse[ep_key])))
        return "\n".join(lines) + "\n"


def get_flow_doc_renderer():
    """The configured FLOW_DOC_RENDERER instance (STAPEL_FLOWS seam)."""
    from .conf import flows_settings

    return flows_settings.FLOW_DOC_RENDERER()


# Backward-compatible module-level entry points (delegate to the default
# renderer). ``language`` localizes the renderer chrome; ``None`` → English.
_default_renderer = DefaultFlowDocRenderer()


def render_flow_markdown(
    flow: Flow,
    index: dict[str, list[EndpointInfo]],
    texts: dict[str, str] | None = None,
    language: str | None = None,
) -> str:
    """Render one flow as markdown (see :class:`DefaultFlowDocRenderer`).

    *texts* is an i18n key → text mapping (see ``flows.i18n.
    resolve_flow_texts``); missing keys fall back to the in-code literals,
    so literal-only flows and callers render exactly as before. *language*
    localizes the renderer's own scaffolding words.
    """
    return _default_renderer.render_flow(flow, index, texts, language)


def render_index_markdown(
    flows: list[Flow],
    index,
    texts: dict[str, str] | None = None,
    language: str | None = None,
) -> str:
    return _default_renderer.render_index(flows, index, texts, language)


def export_json(flows: list[Flow], index) -> str:
    """flows.json — the language-agnostic machine artifact (§2).

    Carries the i18n keys plus the canonical source literals (structure +
    API bindings are one contract; language lives on the presentation layer
    where the keys are resolved).
    """
    payload = []
    for f in flows:
        steps = []
        for s in f.sorted_steps():
            entry = {
                "kind": s.kind, "order": s.order,
                "note": s.note, "note_key": s.note_key, "ref": s.ref,
            }
            if s.kind == STEP_HTTP:
                entry["endpoints"] = [
                    {"method": ep.method, "path": ep.path}
                    for ep in index.get(s.ref, [])
                ]
            steps.append(entry)
        payload.append({
            "id": f.id,
            "title": f.title, "title_key": f.title_key,
            "description": f.description, "description_key": f.description_key,
            "actors": f.actors, "steps": steps,
        })
    return json.dumps(payload, ensure_ascii=False, indent=2)
