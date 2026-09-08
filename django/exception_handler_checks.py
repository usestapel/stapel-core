"""System checks (tag ``stapel_error_envelope``) — the DRF refusal envelope
is only as real as the handler a deployment actually wired.

Since 0.61.0 twelve DRF refusal types — the ones **no view code raises**:
401/403 from authenticators and permission classes, 404 from
``get_object_or_404``, 405/406/415 from dispatch, 429 from a throttle, the
400 of an unparseable body, a 500, and core's own mandate refusal — answer
the fleet's ``{localizable_error, error, params, error_language}`` body
instead of DRF's bare ``{"detail": ...}``. All twelve reach that body through
exactly one seam: ``REST_FRAMEWORK["EXCEPTION_HANDLER"]``.

Core sets that key in ``stapel_core.django.settings`` and in
``stapel_core.testing``, and nothing ever verified that a deployment's
*effective* setting still carries it. A project that writes its own
``REST_FRAMEWORK`` dict — the ordinary thing to do, and what a settings module
does the moment it wants one different renderer — silently drops the key, DRF
falls back to ``rest_framework.views.exception_handler``, and every refusal of
every installed module answers outside the envelope. Nothing raises, nothing
logs, and every test that builds its own error response still passes: the only
symptom is a frontend error path that cannot translate a 401. Found for real
while adopting 0.61.0 — a library's own test settings had this hole, so its
suite could assert an envelope only where a view had hand-built one.

Checks
------
E001  ``REST_FRAMEWORK["EXCEPTION_HANDLER"]`` does not resolve to a callable.
      DRF imports that dotted path lazily, so a typo here is not a boot
      failure: it is an ``ImportError`` raised *inside* exception handling, on
      the first request some layer refuses, and the client gets a 500 in place
      of the refusal. Error, because no reading of this configuration works.
W001  the effective handler is neither core's nor anything that reaches it —
      most often because the key is absent and DRF's own default took over.
      Warning, matching ``stapel_core.error_pages.W001``, which reports the
      other half of exactly this symptom (an API path answering Django's HTML
      error page): the deployment serves, and what degrades is the *shape* of
      refusal bodies. An Error would block the deploy of a host that answers
      correctly on every path a view wrote by hand, and a whole tag in
      ``SILENCED_SYSTEM_CHECKS`` protects nobody.

How a legitimate wrapper is recognised
---------------------------------------
A host may wrap the handler — add a header, log the refusal, convert one of
its own exception types first — and such a wrapper must not be reported: it
ends up calling core's handler, so the envelope survives. **A name proves
nothing** (a wrapper is called anything; a foreign handler may be called
``stapel_exception_handler``), so this imports the configured callable and
inspects it, in four widening steps, none of which consults the handler's own
name:

1. **identity** — it *is* :func:`stapel_exception_handler`;
2. **unwrapping** — ``functools.wraps`` (``__wrapped__``), ``functools.partial``
   (``.func``), a bound method (``__func__``) and a callable object
   (``type(obj).__call__``) are followed to what they stand in front of;
3. **reference by identity** — :func:`inspect.getclosurevars` reports the
   globals and closure cells the callable's code actually reads. Core's
   handler among them means the wrapper's source names it; a module among them
   is looked through by the attribute names in ``co_names``, which is how
   ``errors.stapel_exception_handler(exc, context)`` is seen after a
   module-level import. Functions found this way are followed one further
   level, so a two-deep chain of wrappers still passes;
4. **import evidence in the code object** — for the deferred import (the
   shape a wrapper uses to dodge app-loading order), identity is not available
   without running the import, so the code object is read instead: the exact
   dotted path in ``co_consts`` (``import_string(...)``), or the target's own
   ``__name__`` in ``co_names`` *together with* a reference to the module that
   defines it. Both halves are required — the symbol name alone is never
   enough, which is what keeps this from decaying into the name check step 0
   would have been.

What this honestly cannot see is a wrapper that *computes* its delegate — out
of a registry, a setting, a list built elsewhere. There is no dataflow
analysis here and there will not be one, so those declare themselves, in one
greppable attribute::

    def my_handler(exc, context):
        ...

    my_handler.stapel_delegates_to_exception_handler = True

The declaration is deliberately a claim its author makes rather than a fact
this module verifies — the same bargain ``stapel_anonymous_access`` strikes in
``stapel_core.adoption``. It is the one thing here that is taken on trust, and
it is not a name.
"""
from __future__ import annotations

import functools
import inspect
from typing import Any, Optional

from django.core import checks

E001_HANDLER_UNUSABLE = "stapel_core.error_envelope.E001"
W001_HANDLER_BYPASSED = "stapel_core.error_envelope.W001"

#: The dotted path core's own settings presets carry, and the string a wrapper
#: that resolves its delegate with ``import_string`` contains.
CORE_HANDLER_PATH = "stapel_core.django.api.errors.stapel_exception_handler"

#: The module that defines it, and the parent packages an ``import`` statement
#: may name instead — step 4's "which module was imported" half.
_HANDLER_MODULE, _HANDLER_NAME = CORE_HANDLER_PATH.rsplit(".", 1)

#: The one-attribute declaration for a wrapper whose delegation is computed
#: rather than written (see the module docstring).
DELEGATION_ATTR = "stapel_delegates_to_exception_handler"

#: How many wrapper levels :func:`reaches_core_handler` follows. Two nested
#: wrappers is already unusual; the bound is what keeps a cyclic reference
#: from hanging ``manage.py check``.
_MAX_DEPTH = 3


def _core_handler():
    from stapel_core.django.api.errors import stapel_exception_handler

    return stapel_exception_handler


def effective_handler() -> Any:
    """The handler DRF will actually call, as *this* deployment resolves it.

    ``rest_framework.settings.api_settings`` is the authority on purpose: it
    layers the project's ``REST_FRAMEWORK`` over DRF's defaults and performs
    the dotted-path import, so an absent key reads here as DRF's own
    ``exception_handler`` — which is precisely the silent degradation this
    check exists to report. Reading ``settings.REST_FRAMEWORK`` directly would
    miss it; reading core's own preset would answer a question nobody asked.

    Raises whatever the import raises; E001 is that case.
    """
    from rest_framework.settings import api_settings

    return api_settings.EXCEPTION_HANDLER


def _unwrap(candidate: Any) -> Optional[Any]:
    """One step of "what does this callable stand in front of?"."""
    if isinstance(candidate, functools.partial):
        return candidate.func
    wrapped = getattr(candidate, "__wrapped__", None)
    if wrapped is not None:
        return wrapped
    func = getattr(candidate, "__func__", None)
    if func is not None:
        return func
    if not inspect.isroutine(candidate):
        call = getattr(type(candidate), "__call__", None)
        if call is not None and inspect.isroutine(call):
            return call
    return None


def _referenced_objects(candidate: Any) -> list:
    """Globals, closure cells and module attributes the callable's code reads.

    :func:`inspect.getclosurevars` answers with the names the *code object*
    actually references, so this is not "anything importable from that module"
    — it is what the wrapper was written to touch. Module values are looked
    through by ``co_names`` because ``errors.stapel_exception_handler(...)``
    binds only ``errors`` as a global.
    """
    try:
        closure = inspect.getclosurevars(candidate)
    except (TypeError, ValueError):  # not a Python function, or a builtin
        return []
    values = list(closure.globals.values()) + list(closure.nonlocals.values())
    names = _code_names(candidate)
    for value in list(values):
        if inspect.ismodule(value):
            for name in names:
                attribute = getattr(value, name, None)
                if attribute is not None:
                    values.append(attribute)
    return values


def _code_names(candidate: Any) -> tuple:
    code = getattr(candidate, "__code__", None)
    return tuple(getattr(code, "co_names", ()) or ())


def _code_strings(candidate: Any) -> tuple:
    code = getattr(candidate, "__code__", None)
    consts = getattr(code, "co_consts", ()) or ()
    return tuple(c for c in consts if isinstance(c, str))


def _imports_core_handler(candidate: Any) -> bool:
    """Step 4 — the callable's own code imports the delegate.

    Covers what identity cannot reach: an import written inside the function
    body. ``import_string(CORE_HANDLER_PATH)`` leaves the whole dotted path in
    ``co_consts``; every ``from ... import`` shape leaves the symbol name in
    ``co_names`` next to the module (or a parent package) it came from, and
    **both** are required. The symbol name on its own is not evidence — that
    would be the name check this module refuses to be.
    """
    names = _code_names(candidate)
    strings = _code_strings(candidate)
    if CORE_HANDLER_PATH in strings:
        return True
    if _HANDLER_NAME not in names:
        return False
    for name in names + strings:
        if name == _HANDLER_MODULE or _HANDLER_MODULE.startswith(f"{name}."):
            return True
    return False


def reaches_core_handler(candidate: Any, _depth: int = _MAX_DEPTH) -> bool:
    """Does calling *candidate* end up in core's exception handler?

    See the module docstring for the four widening steps, the declared escape,
    and what this deliberately cannot see. Never consults a name on its own.
    """
    if candidate is None or _depth <= 0:
        return False
    target = _core_handler()
    if candidate is target:
        return True
    if getattr(candidate, DELEGATION_ATTR, False):
        return True
    if _imports_core_handler(candidate):
        return True

    inner = _unwrap(candidate)
    if inner is not None and reaches_core_handler(inner, _depth - 1):
        return True

    for value in _referenced_objects(candidate):
        if value is target:
            return True
        if inspect.isroutine(value) or isinstance(value, functools.partial):
            if reaches_core_handler(value, _depth - 1):
                return True
    return False


def _dotted(candidate: Any) -> str:
    module = getattr(candidate, "__module__", None)
    name = (
        getattr(candidate, "__qualname__", None)
        or getattr(candidate, "__name__", None)
    )
    if module and name:
        return f"{module}.{name}"
    return repr(candidate)


def _unusable(message: str):
    return checks.Error(
        message,
        hint=f"Point it at '{CORE_HANDLER_PATH}' — what "
             f"stapel_core.django.settings ships — or at a callable of your "
             f"own that ends up calling it.",
        id=E001_HANDLER_UNUSABLE,
    )


@checks.register("stapel_error_envelope")
def check_exception_handler_wired(app_configs=None, **kwargs):
    """E001/W001 — see the module docstring."""
    try:
        import rest_framework  # noqa: F401
    except Exception:  # pragma: no cover - DRF not installed
        return []

    try:
        handler = effective_handler()
    except Exception as exc:
        return [_unusable(
            f"REST_FRAMEWORK['EXCEPTION_HANDLER'] cannot be resolved: "
            f"{type(exc).__name__}: {exc}. DRF imports that path lazily, so "
            f"this is not a boot failure — it is an exception raised inside "
            f"exception handling, on the first request any layer refuses, and "
            f"the client gets a 500 in place of the 401/403/404/429 that was "
            f"meant."
        )]

    if not callable(handler):
        return [_unusable(
            f"REST_FRAMEWORK['EXCEPTION_HANDLER'] resolved to {handler!r}, "
            f"which is not callable. Every refused request then raises inside "
            f"exception handling and answers 500 instead of the refusal it was "
            f"going to answer."
        )]

    if reaches_core_handler(handler):
        return []

    return [checks.Warning(
        f"REST_FRAMEWORK['EXCEPTION_HANDLER'] is {_dotted(handler)}, which "
        f"does not reach stapel_exception_handler: the refusals no view code "
        f"raises — 401 and 403 from authenticators and permission classes, 404 "
        f"from get_object_or_404, 405/406/415 from dispatch, 429 from a "
        f"throttle — answer DRF's bare {{\"detail\": \"...\"}} instead of the "
        f"fleet envelope. A frontend that reads 'localizable_error' finds "
        f"nothing there and cannot translate the refusal, and one status comes "
        f"back in two different shapes depending on which layer said no.",
        hint=f"Set REST_FRAMEWORK['EXCEPTION_HANDLER'] = '{CORE_HANDLER_PATH}'. "
             f"A project that writes its own REST_FRAMEWORK dict must carry "
             f"the key: star-importing stapel_core.django.settings replaces "
             f"the dict, it does not merge it. If this handler DOES delegate "
             f"to core's by a route no reader of its source can see (a "
             f"registry, a settings lookup), declare that: "
             f"handler.{DELEGATION_ATTR} = True.",
        id=W001_HANDLER_BYPASSED,
    )]


__all__ = [
    "CORE_HANDLER_PATH",
    "DELEGATION_ATTR",
    "E001_HANDLER_UNUSABLE",
    "W001_HANDLER_BYPASSED",
    "check_exception_handler_wired",
    "effective_handler",
    "reaches_core_handler",
]
