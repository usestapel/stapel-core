"""One walk of the URLconf, shared by every check that reasons about the
*actual HTTP surface* of a deployment.

There is a family of checks whose question has the same shape: "setting X is
on / module Y is installed — does the surface this process really serves match
that?" (BACKLOG §37 mount containment, the ``stapel_adoption`` anonymous-axis
check, …). Each of them needs the same three primitives: walk the resolver
down to leaf patterns, know the full path of each leaf, and know which view
class (and which Stapel module) is behind it.

Written once, those primitives are a mechanism; written twice they are two
private helpers that drift. They lived as ``_iter_url_patterns`` /
``_path_segments`` / ``_callback_owner_app_label`` inside
:mod:`stapel_core.django.mounts` and are re-exported from there unchanged, so
nothing that imported them breaks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Optional


def iter_url_patterns(patterns, prefix: str = ""):
    """Depth-first walk of a URLconf pattern list.

    Yields ``(full_path, url_pattern)`` for every leaf ``URLPattern`` —
    ``full_path`` is the best-effort concatenation of every ancestor route
    string down to this pattern. Good enough to test for the *presence* of a
    canonical path segment and to name a view's location in a check message,
    not a guarantee of the exact browser-facing URL: ``path()`` routes
    concatenate cleanly; a ``re_path()`` ancestor contributes its raw regex
    source (anchors/groups and all) — no stapel module in this repository uses
    ``re_path()`` for its own mount, so this is not a practical gap today.
    """
    from django.urls import URLPattern, URLResolver

    for entry in patterns:
        full = f"{prefix}{entry.pattern}"
        if isinstance(entry, URLResolver):
            yield from iter_url_patterns(entry.url_patterns, full)
        elif isinstance(entry, URLPattern):
            yield full, entry


def path_segments(full_path: str) -> list:
    """Non-empty ``/``-delimited segments of *full_path*, regex anchors
    stripped — good enough for exact-token membership tests
    (``"api" in segments``), not for reconstructing a real URL.

    Anchors are stripped PER SEGMENT, not once across the whole path. A
    module that registers a `re_path` router (stapel-currencies uses
    ``r"api/v1"``) and is then mounted under a host prefix produces
    ``currencies/^api/v1/...`` — the ``^`` lands mid-path, so a single
    ``full_path.strip("^$")`` leaves the segment reading ``"^api"`` and the
    §37 containment check reports E004 against a mount that is perfectly
    canonical. Every client who picked `currencies` got a generated project
    that failed its own `manage.py check` (found 2026-07-26, via the
    scaffold's own gate once an unrelated E003 stopped masking it).
    """
    return [seg.strip("^$") for seg in full_path.split("/") if seg.strip("^$")]


def view_of(callback) -> Any:
    """The view *class* behind a URLconf callback, or the callback itself.

    Class-based views keep the class on the view function (``view_class`` —
    plain Django, ``cls`` — DRF's ``APIView.as_view()``); function-based
    views and lambdas are used as-is. DRF's ``@api_view`` decorator also
    leaves a ``cls`` (a generated ``WrappedAPIView``), so a function-based
    DRF endpoint is introspectable exactly like a class-based one.
    """
    return getattr(callback, "view_class", None) or getattr(callback, "cls", None) or callback


def callback_owner_app_label(callback) -> Optional[str]:
    """The ``app_label`` of the Stapel module that owns *callback*, or
    ``None`` when it belongs to no installed Stapel module (a host's own
    view, or a third-party one).

    Ownership is decided the same way module discovery is
    (:func:`stapel_core.django.nav.is_stapel_app` plus the view's
    ``__module__`` dotted-path prefix against each installed Stapel app's
    ``AppConfig.name`` — covers both the ``stapel_*`` pip packages and a
    project's own marked ``apps/*``).
    """
    from django.apps import apps as django_apps

    from .nav import is_stapel_app

    module_name = getattr(view_of(callback), "__module__", "") or ""
    if not module_name:
        return None
    for app_config in django_apps.get_app_configs():
        if not is_stapel_app(app_config):
            continue
        name = app_config.name
        if module_name == name or module_name.startswith(f"{name}."):
            return app_config.label
    return None


@dataclass(frozen=True)
class SurfaceView:
    """One leaf URL pattern of this deployment, resolved to its view."""

    #: Best-effort concatenated route of every ancestor down to this leaf.
    full_path: str
    #: The view class (or plain callable) behind the pattern.
    view: Any
    #: The URLPattern itself, for checks that need the raw entry.
    pattern: Any

    @property
    def app_label(self) -> Optional[str]:
        """``app_label`` of the owning Stapel module, or None."""
        return callback_owner_app_label(self.pattern.callback)

    @property
    def dotted_name(self) -> str:
        """``package.module.ViewClass`` — how a human finds this view."""
        module = getattr(self.view, "__module__", "") or ""
        name = getattr(self.view, "__qualname__", None) or getattr(
            self.view, "__name__", repr(self.view)
        )
        return f"{module}.{name}" if module else str(name)


def iter_surface(patterns=None) -> Iterator[SurfaceView]:
    """Every leaf URL pattern of this deployment, as :class:`SurfaceView`.

    Yields nothing when the process has no ``ROOT_URLCONF`` (a standalone
    package test harness) — a check built on this never has to special-case
    that itself.
    """
    if patterns is None:
        from django.conf import settings

        if not getattr(settings, "ROOT_URLCONF", ""):
            return
        from django.urls import get_resolver

        patterns = get_resolver().url_patterns

    for full_path, pattern in iter_url_patterns(patterns):
        yield SurfaceView(full_path=full_path, view=view_of(pattern.callback),
                          pattern=pattern)


__all__ = [
    "SurfaceView",
    "callback_owner_app_label",
    "iter_surface",
    "iter_url_patterns",
    "path_segments",
    "view_of",
]
