"""Flow → markdown SA-documentation renderer.

Resolves HTTP steps against the live URLConf: method + path, serializer
classes (via the view's seam attributes when present), permissions and the
step-up verification contract (x-stapel-verification attribute). Non-HTTP
steps render from the comm registries/schemas.
"""
from __future__ import annotations

import json
import logging
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
                    handler = getattr(cls, action, None)
                    ref = _callable_ref(handler, cls, action)
                    endpoints.append(EndpointInfo(http_method.upper(), path, ref, cls))
            elif cls is not None:
                for http_method in ("get", "post", "put", "patch", "delete"):
                    handler = getattr(cls, http_method, None)
                    if handler is None:
                        continue
                    ref = _callable_ref(handler, cls, http_method)
                    endpoints.append(EndpointInfo(http_method.upper(), path, ref, cls))
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


def render_flow_markdown(flow: Flow, index: dict[str, list[EndpointInfo]]) -> str:
    lines = [f"# {flow.title}", ""]
    lines += [f"`{flow.id}`", ""]
    if flow.actors:
        lines += ["**Актор(ы):** " + ", ".join(flow.actors), ""]
    lines += [flow.description.strip(), "", "## Шаги", ""]

    for i, step in enumerate(flow.sorted_steps(), 1):
        if step.kind == STEP_HTTP:
            eps = index.get(step.ref, [])
            if eps:
                for ep in eps[:1]:
                    lines.append(f"{i}. **{ep.method} `{ep.path}`** — {step.note}")
                    if ep.view_cls is not None:
                        sers = _serializer_names(ep.view_cls)
                        if sers:
                            pretty = ", ".join(f"{k.split('_')[0]}: {v}" for k, v in sers.items())
                            lines.append(f"   - сериализаторы: {pretty}")
                        from stapel_core.verification.decorators import (
                            view_verification_contract,
                        )

                        verification = view_verification_contract(ep.view_cls)
                        if verification:
                            factors = verification.get("factors") or ["(defaults)"]
                            lines.append(
                                f"   - требуется верификация: scope=`{verification['scope']}`, "
                                f"факторы: {', '.join(factors)}"
                            )
            else:
                lines.append(f"{i}. **HTTP** `{step.ref}` — {step.note} "
                             f"_(эндпоинт не найден в URLConf)_")
        elif step.kind in (STEP_ACTION, STEP_FUNCTION, STEP_TASK):
            kind_label = {STEP_ACTION: "Action", STEP_FUNCTION: "Function",
                          STEP_TASK: "Task"}[step.kind]
            lines.append(f"{i}. **{kind_label} `{step.ref}`** — {step.note}")
        elif step.kind == STEP_HUMAN:
            lines.append(f"{i}. **Действие пользователя** — {step.note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_index_markdown(flows: list[Flow], index) -> str:
    lines = ["# Флоу", "", "| ID | Название | Шагов |", "|---|---|---|"]
    for f in flows:
        lines.append(f"| [`{f.id}`]({f.id}.md) | {f.title} | {len(f.steps)} |")
    lines += ["", "## Эндпоинт → флоу", ""]
    reverse: dict[str, set[str]] = {}
    for f in flows:
        for step in f.steps:
            if step.kind == STEP_HTTP:
                for ep in index.get(step.ref, []):
                    reverse.setdefault(f"{ep.method} {ep.path}", set()).add(f.id)
    for ep_key in sorted(reverse):
        lines.append(f"- `{ep_key}` → " + ", ".join(sorted(reverse[ep_key])))
    return "\n".join(lines) + "\n"


def export_json(flows: list[Flow], index) -> str:
    payload = []
    for f in flows:
        steps = []
        for s in f.sorted_steps():
            entry = {"kind": s.kind, "order": s.order, "note": s.note, "ref": s.ref}
            if s.kind == STEP_HTTP:
                entry["endpoints"] = [
                    {"method": ep.method, "path": ep.path}
                    for ep in index.get(s.ref, [])
                ]
            steps.append(entry)
        payload.append({
            "id": f.id, "title": f.title, "description": f.description,
            "actors": f.actors, "steps": steps,
        })
    return json.dumps(payload, ensure_ascii=False, indent=2)
