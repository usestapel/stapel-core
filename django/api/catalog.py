"""Auto-regenerated presenter/swap catalog — PRESENTERS.MD (§55 spec §4).

The extensibility contract (``docs/pending/extensibility-presenters.md``)
promises a host two things it can *read* instead of grepping library code:
which classes are swappable through ``STAPEL_SWAP`` (and what the defaults
are), and which presenters exist over which DAO models with which fields.
This module is the single source for both — pure introspection over the two
registries that already exist at runtime:

- :func:`stapel_core.django.swappable.declared_swaps` — every swap point a
  library declared (``declare_swap``) or exercised (``get_model`` /
  ``get_presenter``);
- :func:`stapel_core.django.api.presenters.all_presenters` — every concrete
  :class:`~stapel_core.django.api.presenters.Presenter` subclass imported so
  far, with its generated ``dto`` dataclass.

Nothing here is written by hand: regeneration at release time (the same
REL-freshness discipline as CONFIG.MD / flow docs) keeps the file honest —
a catalog that drifts from the code is a red release, not a stale doc.

Entry points
------------
- :func:`autodiscover_presenters` — import ``<app>.presenters`` for every
  installed app (django-admin style), so registries are populated;
- :func:`presenter_catalog` — the introspected entries, as data;
- :func:`render_presenters_md` — entries -> markdown text;
- :func:`write_presenters_md` — the whole pipeline, to a file. This is also
  the hook ``stapel-tools``' scaffold generator calls in the next wave: the
  library API deliberately does not depend on the management command.

CLI: ``python manage.py presenter_catalog [--out PRESENTERS.MD]``.
"""
from __future__ import annotations

import dataclasses
import typing
from pathlib import Path
from typing import Any, Optional

from stapel_core.django.api.presenters import Presenter, all_presenters
from stapel_core.django.swappable import declared_swaps

PRESENTERS_MD = "PRESENTERS.MD"


@dataclasses.dataclass(frozen=True)
class CatalogField:
    """One DTO field row: name, rendered type, source, description."""

    name: str
    type: str
    source: str
    description: str


@dataclasses.dataclass(frozen=True)
class CatalogEntry:
    """One presenter: its dotted path, swap point (if declared), model, DTO."""

    presenter: str          # dotted path of the default class
    swap_key: Optional[str]  # STAPEL_SWAP key, None when not declared swappable
    model: str              # "app_label.ModelName"
    dto: str                # generated dataclass name
    fields: tuple[CatalogField, ...]


def autodiscover_presenters() -> int:
    """Import ``presenters`` from every installed app (django-admin style),
    so every library's Presenter subclasses and ``declare_swap`` calls have
    run. Returns the number of apps that provided a presenters module.
    Idempotent — repeated imports are no-ops thanks to ``sys.modules``."""
    import importlib

    from django.apps import apps

    count = 0
    for app_config in apps.get_app_configs():
        module_name = f"{app_config.name}.presenters"
        try:
            importlib.import_module(module_name)
            count += 1
        except ModuleNotFoundError as exc:
            if exc.name != module_name:  # real import error inside the module
                raise
    return count


def _dotted(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _render_type(tp: Any) -> str:
    """Human-readable form of a DTO field annotation (`str`, `uuid.UUID`,
    `list[XDTO]`, `Any`, ...)."""
    if tp is Any:
        return "Any"
    origin = typing.get_origin(tp)
    if origin is not None:
        args = ", ".join(_render_type(a) for a in typing.get_args(tp))
        name = getattr(origin, "__name__", str(origin))
        return f"{name}[{args}]"
    if isinstance(tp, type):
        return tp.__name__
    return str(tp)


def _render_source(source: Any) -> str:
    if callable(source):
        return "computed"
    return str(source)


def _model_label(model: type) -> str:
    meta = getattr(model, "_meta", None)
    if meta is not None:
        return f"{meta.app_label}.{model.__name__}"
    return _dotted(model)


def _entry_for(presenter: type[Presenter], swaps_by_default: dict[str, str]) -> CatalogEntry:
    fields: list[CatalogField] = []
    for f in dataclasses.fields(presenter.dto):
        fields.append(CatalogField(
            name=f.name,
            type=_render_type(f.type),
            source=_render_source(presenter._field_sources.get(f.name, f.name)),
            description=str(f.metadata.get("help_text", "")),
        ))
    dotted = _dotted(presenter)
    return CatalogEntry(
        presenter=dotted,
        swap_key=swaps_by_default.get(dotted),
        model=_model_label(presenter.model),
        dto=presenter.dto.__name__,
        fields=tuple(fields),
    )


def presenter_catalog(*, autodiscover: bool = True) -> list[CatalogEntry]:
    """Introspect the registries into catalog entries, one per concrete
    presenter (import order). With ``autodiscover`` (default) every installed
    app's ``presenters`` module is imported first."""
    if autodiscover:
        autodiscover_presenters()
    swaps_by_default = {default: key for key, default in declared_swaps().items()}
    return [_entry_for(p, swaps_by_default) for p in all_presenters()]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_presenters_md(
    entries: list[CatalogEntry],
    *,
    swaps: Optional[dict[str, str]] = None,
    title: str = "PRESENTERS.MD",
) -> str:
    """Render the catalog: one swap-point table + one section per presenter
    (model, DTO, field table). ``swaps`` defaults to the live
    :func:`declared_swaps` snapshot — passed explicitly in tests."""
    if swaps is None:
        swaps = declared_swaps()
    presenter_defaults = {e.presenter for e in entries}

    lines: list[str] = [
        f"# {title} — swappable classes and presenters",
        "",
        "Auto-generated by `manage.py presenter_catalog` "
        "(`stapel_core.django.api.catalog`) — regenerate, don't edit. "
        "A host replaces any class below through the `STAPEL_SWAP` setting "
        "(`docs/pending/extensibility-presenters.md`): its own subclass, "
        "no fork of the owning library.",
        "",
        "## Swap points (`STAPEL_SWAP`)",
        "",
    ]
    if swaps:
        lines += [
            "| Key | Default class | Kind |",
            "|-----|---------------|------|",
        ]
        for key in sorted(swaps):
            default = swaps[key]
            kind = "presenter" if default in presenter_defaults else "model/class"
            lines.append(f"| `{key}` | `{default}` | {kind} |")
    else:
        lines.append("*(no swap points declared)*")
    lines.append("")

    lines += ["## Presenters", ""]
    if not entries:
        lines += ["*(no presenters registered)*", ""]
    for e in entries:
        swap = f"`{e.swap_key}`" if e.swap_key else "— (not swappable via config)"
        lines += [
            f"### `{e.presenter}`",
            "",
            f"- **Model:** `{e.model}`",
            f"- **DTO:** `{e.dto}`",
            f"- **Swap key:** {swap}",
            "",
            "| Field | Type | Source | Description |",
            "|-------|------|--------|-------------|",
        ]
        for f in e.fields:
            lines.append(
                f"| `{f.name}` | `{f.type}` | {_md_escape(f.source)} | "
                f"{_md_escape(f.description)} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_presenters_md(
    out: Path | str = PRESENTERS_MD,
    *,
    title: str = "PRESENTERS.MD",
) -> Path:
    """Autodiscover, introspect, render, write. Returns the written path.

    This function (not the management command) is the seam the scaffold
    generator / release gate uses — callable without argv plumbing."""
    entries = presenter_catalog()
    text = render_presenters_md(entries, title=title)
    path = Path(out)
    path.write_text(text, encoding="utf-8")
    return path


__all__ = [
    "PRESENTERS_MD",
    "CatalogEntry",
    "CatalogField",
    "autodiscover_presenters",
    "presenter_catalog",
    "render_presenters_md",
    "write_presenters_md",
]
