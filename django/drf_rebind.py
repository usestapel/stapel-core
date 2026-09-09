"""DRF binds settings onto class attributes at import time; this rebinds them.

The trap
--------
``rest_framework/views.py`` reads its policy defaults in the class body::

    class APIView(View):
        renderer_classes = api_settings.DEFAULT_RENDERER_CLASSES
        ...
        metadata_class = api_settings.DEFAULT_METADATA_CLASS

Every one of those runs once, when the module is imported, and never again.
A project built on this core imports DRF **from inside its settings module**
— ``config/settings/base.py`` does ``import stapel_core.django``, which
reaches ``rest_framework.views`` through the OpenAPI seam — and it does so
*above* its own ``REST_FRAMEWORK = {...}`` assignment. At that moment
``django.conf.settings`` is a half-built ``Settings`` object (Django sets
``_wrapped`` only after the module finishes, so an access from inside it
re-enters ``_setup()`` and reads the attributes defined *so far*), the key
does not exist yet, and DRF binds its own defaults.

The deployment then sets ``DEFAULT_METADATA_CLASS`` and gets no error, no
warning and no effect. Measured on a client stand, 2026-09-09: a host set
that key to work around an ``OPTIONS`` failure, the key was inert, and the
workaround had to be a line of monkey-patching in the project's own
``AppConfig.ready`` before it did anything.

What is repaired, and how the set is decided
--------------------------------------------
``CommonDjangoConfig.ready`` used to rewrite exactly two of them,
``authentication_classes`` and ``permission_classes``, hand-listed. That list
was already three years out of date: DRF 3.17 binds **eight** attributes on
``APIView`` and two more on ``GenericAPIView`` (``filter_backends``,
``pagination_class`` — the second one silently turns off a deployment's
pagination), plus ``DEFAULT_THROTTLE_RATES`` on ``SimpleRateThrottle`` (a
rate limit configured and not applied), ``PAGE_SIZE`` on three paginators,
the search/ordering query-parameter names on two filters, the version
parameters, and the JSON renderer/parser strictness flags — twenty-four in
all.

So the set is not hand-listed here. :func:`derive_binds` **reads the
installed DRF's source** and reports every ``<attr> = api_settings.<KEY>``
assignment in a class body — which means a DRF upgrade that adds an
attribute is covered on the day it is installed, with no release of this
library. :data:`DECLARED_BINDS` is the floor derivation falls back to when
the source is unreadable (a zipped or source-stripped install); the drift
between the two is a test in this repo (``tests/test_drf_rebind.py``), so a
DRF that adds a bind turns *our* suite red rather than a client's setting
quiet.

Only modules already in ``sys.modules`` are touched. A DRF module imported
*after* the settings module finished binds correctly on its own — there is
nothing to repair, and importing it here to repair it would be this library
inventing work.

The loud half
-------------
Repair alone is still a mechanism that can fail silently, so it is paired
with a system check that verifies the *outcome* rather than this code:
:mod:`stapel_core.django.drf_rebind_checks` compares every derived bind
against the live ``api_settings`` value and errors naming the class, the
attribute and the ``REST_FRAMEWORK`` key that is not in force.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Optional

#: Prefix of the modules this scans. Nothing else is read or written.
DRF_PACKAGE = "rest_framework"

#: The settings singleton's name as DRF's own source spells it. Derivation
#: matches on this name in the class body, not on a value.
SETTINGS_NAME = "api_settings"


@dataclass(frozen=True)
class Bind:
    """One ``<cls>.<attr> = api_settings.<setting>`` written in a class body."""

    module: str
    cls: str
    attr: str
    setting: str

    @property
    def target(self) -> str:
        return f"{self.module}.{self.cls}.{self.attr}"

    def __str__(self) -> str:  # pragma: no cover - repr sugar
        return f"{self.target} = {SETTINGS_NAME}.{self.setting}"


#: The floor: what DRF 3.17.1 binds, verified against the installed source by
#: ``tests/test_drf_rebind.py``. Used only when a module's source cannot be
#: read; derivation from the installed source is the authority.
DECLARED_BINDS: tuple[Bind, ...] = (
    Bind("rest_framework.views", "APIView", "renderer_classes", "DEFAULT_RENDERER_CLASSES"),
    Bind("rest_framework.views", "APIView", "parser_classes", "DEFAULT_PARSER_CLASSES"),
    Bind("rest_framework.views", "APIView", "authentication_classes", "DEFAULT_AUTHENTICATION_CLASSES"),
    Bind("rest_framework.views", "APIView", "throttle_classes", "DEFAULT_THROTTLE_CLASSES"),
    Bind("rest_framework.views", "APIView", "permission_classes", "DEFAULT_PERMISSION_CLASSES"),
    Bind("rest_framework.views", "APIView", "content_negotiation_class", "DEFAULT_CONTENT_NEGOTIATION_CLASS"),
    Bind("rest_framework.views", "APIView", "metadata_class", "DEFAULT_METADATA_CLASS"),
    Bind("rest_framework.views", "APIView", "versioning_class", "DEFAULT_VERSIONING_CLASS"),
    Bind("rest_framework.generics", "GenericAPIView", "filter_backends", "DEFAULT_FILTER_BACKENDS"),
    Bind("rest_framework.generics", "GenericAPIView", "pagination_class", "DEFAULT_PAGINATION_CLASS"),
    Bind("rest_framework.pagination", "PageNumberPagination", "page_size", "PAGE_SIZE"),
    Bind("rest_framework.pagination", "LimitOffsetPagination", "default_limit", "PAGE_SIZE"),
    Bind("rest_framework.pagination", "CursorPagination", "page_size", "PAGE_SIZE"),
    Bind("rest_framework.filters", "SearchFilter", "search_param", "SEARCH_PARAM"),
    Bind("rest_framework.filters", "OrderingFilter", "ordering_param", "ORDERING_PARAM"),
    Bind("rest_framework.versioning", "BaseVersioning", "default_version", "DEFAULT_VERSION"),
    Bind("rest_framework.versioning", "BaseVersioning", "allowed_versions", "ALLOWED_VERSIONS"),
    Bind("rest_framework.versioning", "BaseVersioning", "version_param", "VERSION_PARAM"),
    Bind("rest_framework.renderers", "JSONRenderer", "compact", "COMPACT_JSON"),
    Bind("rest_framework.renderers", "JSONRenderer", "strict", "STRICT_JSON"),
    Bind("rest_framework.parsers", "JSONParser", "strict", "STRICT_JSON"),
    Bind("rest_framework.throttling", "SimpleRateThrottle", "THROTTLE_RATES", "DEFAULT_THROTTLE_RATES"),
    Bind("rest_framework.test", "APIRequestFactory", "renderer_classes_list", "TEST_REQUEST_RENDERER_CLASSES"),
    Bind("rest_framework.test", "APIRequestFactory", "default_format", "TEST_REQUEST_DEFAULT_FORMAT"),
)


def imported_drf_modules() -> list[str]:
    """DRF modules already imported, in import order.

    Import order matters: a bind on a base class must be repaired before one
    on a subclass in a later module, or the subclass's own value is the one
    left standing.
    """
    return [
        name
        for name in list(sys.modules)
        if name == DRF_PACKAGE or name.startswith(f"{DRF_PACKAGE}.")
    ]


def _module_source(name: str) -> Optional[str]:
    """The module's own source, ``""`` when it has none, ``None`` when it
    has some and this process cannot read it (the only case worth reporting).
    """
    module = sys.modules.get(name)
    if module is None:
        return None
    path = getattr(module, "__file__", None)
    if path is None:  # namespace package — no code, so no bind
        return ""
    if not path.endswith(".py"):  # compiled or source-stripped install
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _binds_in_source(module: str, source: str) -> list[Bind]:
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a DRF that does not parse
        return []

    found: list[Bind] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            targets: Iterable[ast.expr]
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            else:
                continue
            value = statement.value
            if not isinstance(value, ast.Attribute):
                continue
            if not isinstance(value.value, ast.Name) or value.value.id != SETTINGS_NAME:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    found.append(Bind(module, node.name, target.id, value.attr))
    return found


def derive_binds(modules: Optional[Iterable[str]] = None) -> tuple[list[Bind], list[str]]:
    """Every class-body ``api_settings`` bind in the DRF modules given.

    Defaults to the DRF modules already imported. Returns the binds found and
    the modules whose source could not be read — the second list is what the
    W-level check reports, because an unreadable module is coverage this
    cannot claim.
    """
    names = list(modules) if modules is not None else imported_drf_modules()
    binds: list[Bind] = []
    unreadable: list[str] = []
    for name in names:
        source = _module_source(name)
        if source is None:
            unreadable.append(name)
            continue
        binds.extend(_binds_in_source(name, source))
    return binds, unreadable


def binds_to_repair() -> tuple[list[Bind], list[str]]:
    """Derived binds, topped up from :data:`DECLARED_BINDS` where unreadable.

    The floor only covers modules that are imported *and* unreadable — a DRF
    module nobody imported has nothing stale in it.
    """
    binds, unreadable = derive_binds()
    if unreadable:
        known = set(unreadable)
        seen = set(binds)
        binds = binds + [
            bind
            for bind in DECLARED_BINDS
            if bind.module in known and bind not in seen
        ]
    return binds, unreadable


def resolve(bind: Bind) -> tuple[Any, Any, bool]:
    """``(class, live api_settings value, the class actually exists)``."""
    module = sys.modules.get(bind.module)
    owner = getattr(module, bind.cls, None) if module is not None else None
    if owner is None or not isinstance(owner, type):
        return None, None, False

    from rest_framework.settings import api_settings

    try:
        desired = getattr(api_settings, bind.setting)
    except (AttributeError, ImportError):
        # A setting DRF no longer defines, or an import string this
        # deployment cannot resolve. The check reports it; repairing it is
        # not this function's business.
        return owner, None, False
    return owner, desired, True


def same_value(current: Any, desired: Any) -> bool:
    """Equality that does not care whether a policy list is a list or a tuple."""
    if isinstance(current, (list, tuple)) and isinstance(desired, (list, tuple)):
        return list(current) == list(desired)
    return current is desired or current == desired


def rebind_api_settings() -> list[Bind]:
    """Write the live ``api_settings`` value back onto every stale bind.

    Returns the binds actually changed. Idempotent, and a no-op in a process
    whose settings module did not spring the trap.
    """
    try:
        import rest_framework  # noqa: F401
        from rest_framework.settings import api_settings
    except Exception:  # pragma: no cover - DRF not installed
        return []

    # DRF caches on first access, and that cache may have been built against
    # a half-loaded settings module too.
    api_settings.reload()

    changed: list[Bind] = []
    binds, _unreadable = binds_to_repair()
    for bind in binds:
        owner, desired, ok = resolve(bind)
        if not ok:
            continue
        # Only a value the class itself owns. An attribute a subclass never
        # rebound is inherited and repaired by fixing the class that did.
        if bind.attr not in owner.__dict__:
            continue
        if same_value(owner.__dict__[bind.attr], desired):
            continue
        setattr(owner, bind.attr, desired)
        changed.append(bind)
    return changed


__all__ = [
    "Bind",
    "DECLARED_BINDS",
    "DRF_PACKAGE",
    "SETTINGS_NAME",
    "binds_to_repair",
    "derive_binds",
    "imported_drf_modules",
    "rebind_api_settings",
    "resolve",
    "same_value",
]
