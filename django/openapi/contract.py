"""Contract profile 2: per-operation facts a generated client can rely on.

The committed ``docs/schema.json`` of a module is emitted under
drf-spectacular defaults (``stapel_tools.codegen``), and that emission is
*profile 1*: byte-for-byte what consumers have pinned. Profile 2 is opt-in
(``stapel_tools.codegen.emit_schema(..., contract_profile=2)``) and adds what
a native client generator needs and profile 1 cannot say:

* **security** — the bearer scheme next to the cookie one (the authenticator
  accepts both), and an anonymous alternative (``{}``) exactly when the
  operation's permission stack admits a caller with no credentials. That
  verdict is obtained by *running* the stack against Django's
  ``AnonymousUser`` — the same probe ``stapel_adoption`` W003 uses — not from
  a list of class names: ``DenyEnrollOnly`` admits a stranger and says so only
  when asked;
* **error codes per operation** — ``responses.<status>["x-stapel-errors"]``,
  derived from the code that can produce them (see :func:`collect_error_codes`)
  plus explicit :func:`raises` declarations;
* **captcha** — ``x-stapel-captcha`` on operations guarded by
  ``@captcha_protected`` or whose request serializer is a ``CaptchaMixin``.

Everything here is read only while :func:`contract_profile` is active, so a
profile-1 emission runs the exact code path it ran before this module existed.
The JSON-level parts of profile 2 (flows, verification, discriminators,
pagination, the schema-less-response refusal) are a post-pass in
``stapel_tools.codegen`` over the finished document.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import dataclasses
import functools
import importlib
import inspect
import re
import types
from contextvars import ContextVar
from http import HTTPStatus
from typing import Any, Callable, Iterable, Iterator, Optional

__all__ = [
    "BEARER_SCHEME",
    "BEARER_SCHEME_NAME",
    "CAPTCHA_ATTR",
    "CONTRACT_PROFILE_KEY",
    "CREDENTIALS_ATTR",
    "Credential",
    "ERRORS_KEY",
    "RAISES_ATTR",
    "SUPPORTED_PROFILES",
    "accepts_credentials",
    "admits_anonymous",
    "captcha_contract",
    "collect_error_codes",
    "contract_profile",
    "current_contract_profile",
    "declared_credentials",
    "raises",
    "registered_error_codes",
]

#: ``info[CONTRACT_PROFILE_KEY]`` records the profile a document was emitted in.
CONTRACT_PROFILE_KEY = "x-stapel-contract"
#: Profiles :func:`contract_profile` accepts. 1 is the historical emission.
SUPPORTED_PROFILES = (1, 2)
#: Per-response list of the error codes an operation can answer with.
ERRORS_KEY = "x-stapel-errors"
#: Per-operation captcha requirement.
CAPTCHA_KEY = "x-stapel-captcha"

#: Attribute :func:`raises` stores its declared codes under.
RAISES_ATTR = "_stapel_raises"
#: Attribute ``@captcha_protected`` stores ``{"action": ...}`` under.
CAPTCHA_ATTR = "_stapel_captcha"
#: Attribute :func:`accepts_credentials` stores its declaration under.
CREDENTIALS_ATTR = "_stapel_credentials"

BEARER_SCHEME_NAME = "JWTBearerAuth"
BEARER_SCHEME = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "JWT",
    "description": (
        "JWT access token in the `Authorization: Bearer <token>` header — the "
        "credential a native client sends. When the JWT cookie is also "
        "present, the cookie wins."
    ),
}

_CODE_RE = re.compile(r"^error\.(\d{3})\.[A-Za-z0-9_.\-]+$")

_PROFILE: ContextVar[int] = ContextVar("stapel_contract_profile", default=1)
#: ``code -> status`` for the running emission, built once per profile block.
_REGISTRY_CACHE: ContextVar[Optional[dict]] = ContextVar(
    "stapel_contract_registry", default=None
)


@contextlib.contextmanager
def contract_profile(profile: int) -> Iterator[int]:
    """Emit under *profile* for the duration of the block."""
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(
            f"unknown contract profile {profile!r}; supported: {SUPPORTED_PROFILES}"
        )
    token = _PROFILE.set(profile)
    registry_token = _REGISTRY_CACHE.set(None)
    try:
        yield profile
    finally:
        _REGISTRY_CACHE.reset(registry_token)
        _PROFILE.reset(token)


def current_contract_profile() -> int:
    """The profile the running emission is in (1 outside :func:`contract_profile`)."""
    return _PROFILE.get()


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def raises(*codes: str) -> Callable:
    """Declare error codes a view handler can answer with.

    For the codes the static derivation cannot see (raised behind a dynamic
    dispatch, built from data)::

        @raises("error.409.slot_taken")
        def post(self, request): ...

    Stacks; usable on a handler or on a view class (applies to every
    operation of it). Each code must be an ``error.<status>.<name>`` key and,
    at emission, a registered one.
    """
    for code in codes:
        if not isinstance(code, str) or not _CODE_RE.match(code):
            raise ValueError(f"@raises: {code!r} is not an error.<status>.<name> key")

    def decorator(target):
        declared = list(getattr(target, RAISES_ATTR, ()) or ())
        declared.extend(c for c in codes if c not in declared)
        setattr(target, RAISES_ATTR, tuple(declared))
        return target

    return decorator


@dataclasses.dataclass(frozen=True)
class Credential:
    """A credential an operation checks in its own body: ``name`` is the
    ``components.securitySchemes`` key, ``scheme`` the OpenAPI security scheme
    object (``{"type": "apiKey", "in": "header", "name": "X-Closure-Token"}``)
    or a zero-argument callable returning it at emission, for a scheme that
    names something the deployment configures (a cookie name in settings)."""

    name: str
    scheme: Any

    def definition(self) -> dict:
        return dict(self.scheme() if callable(self.scheme) else self.scheme)


def accepts_credentials(
    *credentials: Credential, session: bool = True, anonymous: bool = False
) -> Callable:
    """Declare the credentials an operation accepts that no authenticator sees.

    The derived ``security`` knows the session (the JWT cookie, and the bearer
    under profile 2) and whether the permission stack admits a stranger. A
    view that is open at the DRF layer and checks another credential in its
    body (a refresh cookie, a service key, a single-purpose token) reads as
    either public or session-only, and a generated client sends the wrong
    thing. With this declaration the operation's ``security`` is::

        [*session alternatives if session, *{c.name: []} per credential, {} if anonymous]

    and every scheme is registered under ``components.securitySchemes``.
    ``anonymous=True`` keeps the no-credential alternative (a credential the
    body may carry instead, like a refresh token posted in JSON). Applies in
    every profile. Usable on a handler or on a view class.
    """
    for credential in credentials:
        if not isinstance(credential, Credential):
            raise TypeError(f"@accepts_credentials: {credential!r} is not a Credential")
    declaration = {
        "credentials": tuple(credentials),
        "session": bool(session),
        "anonymous": bool(anonymous),
    }

    def decorator(target):
        setattr(target, CREDENTIALS_ATTR, declaration)
        return target

    return decorator


def declared_credentials(view: Any, method: str) -> Optional[dict]:
    """The :func:`accepts_credentials` declaration for *view*'s *method*, if any."""
    handler = _handler_of(view, method)
    return getattr(handler, CREDENTIALS_ATTR, None) or getattr(
        type(view), CREDENTIALS_ATTR, None
    )


def apply_credentials(auto_schema, auths: list, declaration: dict) -> list:
    """Rewrite *auths* by a declaration and register its schemes."""
    from drf_spectacular.plumbing import ResolvedComponent

    out = [a for a in auths if a] if declaration["session"] else []
    for credential in declaration["credentials"]:
        auto_schema.registry.register_on_missing(
            ResolvedComponent(
                name=credential.name,
                type=ResolvedComponent.SECURITY_SCHEMA,
                object=credential.name,
                schema=credential.definition(),
            )
        )
        requirement = {credential.name: []}
        if requirement not in out:
            out.append(requirement)
    if declaration["anonymous"]:
        out.append({})
    return out


def registered_error_codes() -> dict[str, int]:
    """``code -> status`` of every key this instance can raise.

    Imports every app's ``errors`` module first, exactly like
    ``generate_error_keys`` does before writing ``errors.json``, so the set is
    the one that file carries.
    """
    from django.conf import settings
    from django.utils.module_loading import autodiscover_modules

    from stapel_core.django.api.errors import build_error_registry
    from stapel_core.django.management.commands.generate_error_keys import (
        _CORE_ERROR_MODULES,
    )

    autodiscover_modules("errors")
    for mod in (*_CORE_ERROR_MODULES, *getattr(settings, "STAPEL_ERROR_MODULES", [])):
        try:
            importlib.import_module(mod)
        except ImportError:
            continue
    return {e["code"]: e["status"] for e in build_error_registry()}


# ---------------------------------------------------------------------------
# Security: does the permission stack admit a caller with no credentials?
# ---------------------------------------------------------------------------


def _anonymous_request(method: str):
    from django.contrib.auth.models import AnonymousUser
    from django.http import HttpRequest

    request = HttpRequest()
    request.method = method.upper()
    request.path = "/"
    request.META = {
        "REQUEST_METHOD": method.upper(),
        "SERVER_NAME": "contract",
        "SERVER_PORT": "80",
    }
    request.user = AnonymousUser()
    request.auth = None
    request.successful_authenticator = None
    return request


def admits_anonymous(view: Any, method: str) -> bool:
    """Whether *view*'s permission stack lets a credential-less *method* through.

    Runs every permission of ``view.get_permissions()`` (DRF ANDs them) on a
    request carrying ``AnonymousUser`` and no token. A permission that raises
    while probed is a refusal: the verdict "public" is only ever given on
    evidence.
    """
    request = _anonymous_request(method)
    try:
        gates = list(view.get_permissions())
    except Exception:
        return False
    for gate in gates:
        try:
            if not gate.has_permission(request, view):
                return False
        except Exception:
            return False
    return True


# ---------------------------------------------------------------------------
# Error codes: a static walk from the handler through the code it reaches
# ---------------------------------------------------------------------------


def _code_status(code: str) -> Optional[int]:
    match = _CODE_RE.match(code)
    return int(match.group(1)) if match else None


#: What core's exception handler answers Django's ``Http404`` with.
GENERIC_NOT_FOUND_CODE = "error.404.not_found"


@functools.lru_cache(maxsize=1)
def _generic_not_found_functions() -> frozenset:
    """Framework callables that raise ``Http404`` through the generic lookup.

    The walker does not read DRF or Django, so it cannot see that
    ``GenericAPIView.get_object`` (and every mixin handler that calls it)
    ends in ``get_object_or_404``. Reaching one of these is reaching
    :data:`GENERIC_NOT_FOUND_CODE`.
    """
    from django import shortcuts
    from rest_framework import generics, mixins

    candidates = [
        shortcuts.get_object_or_404,
        generics.get_object_or_404,
        generics.GenericAPIView.get_object,
        mixins.RetrieveModelMixin.retrieve,
        mixins.UpdateModelMixin.update,
        mixins.UpdateModelMixin.partial_update,
        mixins.DestroyModelMixin.destroy,
    ]
    return frozenset(getattr(f, "__code__", None) for f in candidates) - {None}


class _CodeWalker:
    """Collect error-code strings reachable from a set of callables.

    Reads source, never runs it. From each function it follows names it can
    resolve statically — module globals, closure cells (decorator wrappers),
    imports inside the function body, ``self.<attr>`` on the owning class,
    ``Module.attr`` / ``Class.attr`` chains and ``Class(...).method`` — into
    functions defined in an allowed package. Every string constant or
    resolved string value that is a registered ``error.<status>.<name>`` key
    is collected.

    It over-approximates (a code referenced on a branch this operation never
    takes is still collected) and it under-approximates (``getattr`` by
    name, methods called on local variables, framework code outside the
    allowed packages are not followed). Callers bind what it finds and say
    nothing about the rest.
    """

    def __init__(self, registry: dict[str, int], packages: Iterable[str]):
        self.registry = registry
        self.packages = tuple(packages)
        self.codes: set[str] = set()
        self.unregistered: set[str] = set()
        self._seen: set[Any] = set()

    # -- entry points ------------------------------------------------------

    def allowed(self, obj: Any) -> bool:
        module = getattr(obj, "__module__", None) or ""
        top = module.split(".", 1)[0]
        return top.startswith("stapel_") or top in self.packages

    def walk(self, obj: Any, owner: Optional[type] = None) -> None:
        func, owner = self._function_of(obj, owner)
        if func is None:
            if isinstance(obj, type):
                self.walk_class(obj)
            return
        key = (func.__code__, owner)
        if key in self._seen:
            return
        self._seen.add(key)
        self._collect_declared(func)
        if func.__code__ in _generic_not_found_functions():
            self._see_value(GENERIC_NOT_FOUND_CODE)
            return
        # A decorator wrapper lives in its decorator's module, which may be
        # outside the allowed set; its closure still leads to the handler.
        for cell_value in self._closure(func).values():
            if callable(cell_value) and not isinstance(cell_value, type):
                self.walk(cell_value, owner)
        if not self.allowed(func):
            return
        tree = self._parse(func)
        if tree is None:
            return
        local_imports = self._local_imports(tree, func)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self._see_value(node.value)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                found, value = self._resolve_name(node.id, func, local_imports)
                if found:
                    self._follow(value)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                found, value, value_owner = self._resolve_attribute(
                    node, func, owner, local_imports
                )
                if found:
                    self._follow(value, value_owner)

    def walk_class(self, cls: type) -> None:
        """A referenced class: its constructor, and for a serializer its validators."""
        if cls in self._seen or not self.allowed(cls):
            return
        self._seen.add(cls)
        self._collect_declared(cls)
        for name, value in vars(cls).items():
            if isinstance(value, str):
                self._see_value(value)
        names = ["__init__", "has_permission", "has_object_permission", "authenticate"]
        try:
            from rest_framework import serializers

            if issubclass(cls, serializers.BaseSerializer):
                names += ["validate", "to_internal_value", "create", "update", "save"]
                names += [n for n in dir(cls) if n.startswith("validate_")]
        except ImportError:  # pragma: no cover - DRF is a core dependency
            pass
        for klass in cls.__mro__:
            if not self.allowed(klass):
                continue
            for name in names:
                if name in vars(klass):
                    self.walk(vars(klass)[name], klass)

    # -- helpers -----------------------------------------------------------

    def _collect_declared(self, target: Any) -> None:
        seen = set()
        while target is not None and id(target) not in seen:
            seen.add(id(target))
            for code in getattr(target, RAISES_ATTR, ()) or ():
                self._see_value(code, declared=True)
            target = getattr(target, "__wrapped__", None)

    def _see_value(self, value: str, declared: bool = False) -> None:
        if not _CODE_RE.match(value):
            return
        if value in self.registry:
            self.codes.add(value)
        elif declared:
            self.unregistered.add(value)

    @staticmethod
    def _function_of(obj: Any, owner: Optional[type]):
        if isinstance(obj, (staticmethod, classmethod)):
            obj = obj.__func__
        if isinstance(obj, property):
            obj = obj.fget
        if isinstance(obj, functools.partial):
            obj = obj.func
        if isinstance(obj, types.MethodType):
            bound = obj.__self__
            owner = bound if isinstance(bound, type) else type(bound)
            obj = obj.__func__
        if isinstance(obj, types.FunctionType):
            return obj, owner
        return None, owner

    @staticmethod
    def _closure(func) -> dict[str, Any]:
        cells = {}
        for name, cell in zip(func.__code__.co_freevars, func.__closure__ or ()):
            try:
                cells[name] = cell.cell_contents
            except ValueError:
                continue
        return cells

    @staticmethod
    def _parse(func) -> Optional[ast.AST]:
        try:
            source = inspect.getsource(func)
        except (OSError, TypeError):
            return None
        # A method's source is indented, and dedent() gives up when a
        # docstring has lines at column 0; nesting it under a block keeps
        # the indentation legal without touching string contents.
        if source[:1].isspace():
            source = "if 1:\n" + source
        try:
            return ast.parse(source)
        except (SyntaxError, ValueError):
            return None

    @staticmethod
    def _local_imports(tree: ast.AST, func) -> dict[str, Any]:
        bound: dict[str, Any] = {}
        for node in ast.walk(tree):
            try:
                if isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        parts = (func.__module__ or "").split(".")
                        anchor = ".".join(parts[: len(parts) - node.level])
                        base = f"{anchor}.{base}" if base else anchor
                    module = importlib.import_module(base)
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        name = alias.asname or alias.name
                        if hasattr(module, alias.name):
                            bound[name] = getattr(module, alias.name)
                        else:
                            bound[name] = importlib.import_module(f"{base}.{alias.name}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        module = importlib.import_module(alias.name)
                        if alias.asname:
                            bound[alias.asname] = module
                        else:
                            top = alias.name.split(".", 1)[0]
                            bound[top] = importlib.import_module(top)
            except Exception:
                continue
        return bound

    def _resolve_name(self, name: str, func, local_imports: dict[str, Any]):
        if name in local_imports:
            return True, local_imports[name]
        closure = self._closure(func)
        if name in closure:
            return True, closure[name]
        if name in func.__globals__:
            return True, func.__globals__[name]
        return False, None

    def _resolve_attribute(self, node: ast.Attribute, func, owner, local_imports):
        chain: list[str] = []
        base: ast.AST = node
        while isinstance(base, ast.Attribute):
            chain.append(base.attr)
            base = base.value
        chain.reverse()
        if isinstance(base, ast.Name):
            if base.id in ("self", "cls") and owner is not None:
                obj: Any = owner
            else:
                found, obj = self._resolve_name(base.id, func, local_imports)
                if not found:
                    return False, None, None
        elif (
            isinstance(base, ast.Call)
            and isinstance(base.func, ast.Name)
            and base.func.id == "super"
            and not base.args
        ):
            # ``super().method`` — the next definition after *func*'s class.
            return self._resolve_super(chain, func, owner)
        elif isinstance(base, ast.Call) and isinstance(base.func, ast.Name):
            # ``Service().method`` — follow the class's method.
            found, obj = self._resolve_name(base.func.id, func, local_imports)
            if not found or not isinstance(obj, type):
                return False, None, None
        else:
            return False, None, None
        holder: Optional[type] = None
        for attr in chain:
            if not isinstance(obj, (types.ModuleType, type)):
                return False, None, None
            holder = obj if isinstance(obj, type) else None
            try:
                obj = inspect.getattr_static(obj, attr)
            except AttributeError:
                return False, None, None
        return True, obj, holder

    @staticmethod
    def _resolve_super(chain: list[str], func, owner: Optional[type]):
        if owner is None or len(chain) != 1:
            return False, None, None
        mro = owner.__mro__
        for index, klass in enumerate(mro):
            defined = vars(klass).values()
            if any(getattr(v, "__code__", None) is func.__code__ for v in defined):
                break
        else:
            return False, None, None
        for klass in mro[index + 1:]:
            if chain[0] in vars(klass):
                return True, vars(klass)[chain[0]], owner
        return False, None, None

    def _follow(self, value: Any, owner: Optional[type] = None) -> None:
        if isinstance(value, str):
            self._see_value(value)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for item in value:
                if isinstance(item, str):
                    self._see_value(item)
        elif isinstance(value, type):
            self.walk_class(value)
        elif isinstance(value, (types.FunctionType, types.MethodType, staticmethod,
                                classmethod, functools.partial, property)):
            self.walk(value, owner)


def _handler_of(view: Any, method: str):
    action = getattr(view, "action", None)
    if action:
        handler = getattr(type(view), action, None)
        if handler is not None:
            return handler
    return getattr(type(view), method.lower(), None)


def _serializer_classes(serializer: Any) -> list[type]:
    if serializer is None:
        return []
    proxied = getattr(serializer, "serializers", None)
    if proxied is not None and not isinstance(serializer, type):
        items = proxied.values() if isinstance(proxied, dict) else proxied
        out: list[type] = []
        for item in items:
            out.extend(_serializer_classes(item))
        return out
    if isinstance(serializer, type):
        return [serializer]
    child = getattr(serializer, "child", None)
    if child is not None and getattr(serializer, "many", False):
        return _serializer_classes(child)
    return [type(serializer)]


def collect_error_codes(
    view: Any,
    method: str,
    *,
    request_serializer: Any = None,
    registry: Optional[dict[str, int]] = None,
) -> tuple[dict[int, list[str]], set[str]]:
    """Error codes an operation can answer with, grouped by HTTP status.

    Sources, in the order they gate a request: the authenticators and the
    permission stack the view runs before dispatch, the handler (through its
    decorators and everything :class:`_CodeWalker` can follow from it), the
    request serializer's validators, and :func:`raises` declarations on the
    handler or the view class.

    Returns ``(by_status, unregistered_declared)`` — the second set names
    codes a :func:`raises` declaration promised that no ``errors`` module
    registers (a source defect the caller should refuse).
    """
    if registry is None:
        registry = registered_error_codes()
    view_cls = type(view)
    own_package = (view_cls.__module__ or "").split(".", 1)[0]
    walker = _CodeWalker(registry, packages=(own_package,))

    try:
        authenticators = list(view.get_authenticators())
    except Exception:
        authenticators = []
    for authenticator in authenticators:
        walker.walk(getattr(type(authenticator), "authenticate", None), type(authenticator))
    try:
        gates = list(view.get_permissions())
    except Exception:
        gates = []
    for gate in gates:
        for name in ("has_permission", "has_object_permission"):
            walker.walk(getattr(type(gate), name, None), type(gate))

    handler = _handler_of(view, method)
    if handler is not None:
        walker.walk(handler, view_cls)
    walker._collect_declared(view_cls)

    for klass in _serializer_classes(request_serializer):
        walker.walk_class(klass)

    by_status: dict[int, list[str]] = {}
    for code in sorted(walker.codes):
        status = registry.get(code) or _code_status(code)
        if status is None:
            continue
        by_status.setdefault(status, []).append(code)
    return dict(sorted(by_status.items())), walker.unregistered


# ---------------------------------------------------------------------------
# Captcha
# ---------------------------------------------------------------------------


def captcha_contract(view: Any, method: str, *, request_serializer: Any = None) -> Optional[dict]:
    """``x-stapel-captcha`` for an operation, or ``None`` when it has none.

    ``@captcha_protected(action=...)`` takes the token from the
    ``X-Captcha-Token`` header or a ``captcha_token`` body field and is
    policy-driven by *action*; a ``CaptchaMixin`` request serializer reads
    only the body field and has no action.
    """
    handler = _handler_of(view, method)
    target, hops = handler, 0
    while target is not None and hops < 16:
        marker = getattr(target, CAPTCHA_ATTR, None)
        if marker:
            return {
                "action": marker.get("action"),
                "token": {"header": "X-Captcha-Token", "field": "captcha_token"},
            }
        target = getattr(target, "__wrapped__", None)
        hops += 1
    try:
        from stapel_core.django.captcha import CaptchaMixin
    except ImportError:  # pragma: no cover
        return None
    for klass in _serializer_classes(request_serializer):
        if isinstance(klass, type) and issubclass(klass, CaptchaMixin):
            return {"action": None, "token": {"field": "captcha_token"}}
    return None


# ---------------------------------------------------------------------------
# AutoSchema seam (PermissionAwareAutoSchema calls these under profile >= 2)
# ---------------------------------------------------------------------------


class ContractProfileError(RuntimeError):
    """The source cannot be described truthfully in the requested profile."""



def _registry() -> dict[str, int]:
    cached = _REGISTRY_CACHE.get()
    if cached is None:
        cached = registered_error_codes()
        _REGISTRY_CACHE.set(cached)
    return cached


#: The code core answers an unauthenticated request with (DRF's
#: ``NotAuthenticated`` through ``stapel_exception_handler``).
NOT_AUTHENTICATED_CODE = "error.401.unauthorized"


def handler_demands_credentials(
    view: Any, method: str, registry: Optional[dict[str, int]] = None
) -> bool:
    """Whether the handler itself answers ``error.401.unauthorized``.

    Some views gate in the body (``if not request.user.is_authenticated:
    return 401``) behind a permission stack that admits strangers; for a
    client that operation needs credentials all the same.
    """
    handler = _handler_of(view, method)
    if handler is None:
        return False
    if registry is None:
        registry = registered_error_codes()
    walker = _CodeWalker(registry, packages=((type(view).__module__ or "").split(".", 1)[0],))
    walker.walk(handler, type(view))
    return NOT_AUTHENTICATED_CODE in walker.codes


def profile_security(auto_schema, auths: list) -> list:
    """Profile-2 ``security``: drop drf-spectacular's ``AllowAny`` guess, ask the gates.

    The anonymous alternative ``{}`` is kept exactly when the permission
    stack admits a credential-less caller (:func:`admits_anonymous`) and the
    handler does not refuse one itself (:func:`handler_demands_credentials`).
    """
    out = [a for a in auths if a != {}]
    view, method = auto_schema.view, auto_schema.method
    if admits_anonymous(view, method) and not handler_demands_credentials(
        view, method, _registry()
    ):
        out.append({})
    return out


def _verification_contract(view: Any, method: str):
    from stapel_core.verification.decorators import VERIFICATION_ATTR

    handler = _handler_of(view, method)
    return getattr(handler, VERIFICATION_ATTR, None) or getattr(
        type(view), VERIFICATION_ATTR, None
    )


def annotate_operation(auto_schema, operation: dict) -> dict:
    """Attach profile-2 per-operation facts to a finished *operation*."""
    from stapel_core.django.api.errors import StapelErrorSerializer

    view, method = auto_schema.view, auto_schema.method
    request_serializer = None
    if method in ("PUT", "PATCH", "POST"):  # the methods spectacular reads a body for
        try:
            request_serializer = auto_schema.get_request_serializer()
        except Exception:
            request_serializer = None

    by_status, unregistered = collect_error_codes(
        view, method, request_serializer=request_serializer, registry=_registry()
    )
    if unregistered:
        raise ContractProfileError(
            f"{operation.get('operationId')}: @raises declares unregistered "
            f"error codes {sorted(unregistered)} — register them in the module's "
            f"errors.py"
        )
    responses = operation.setdefault("responses", {})
    for status, codes in by_status.items():
        key = str(status)
        if key not in responses:
            if status == 403 and _verification_contract(view, method):
                from .extensions import VERIFICATION_403

                responses[key] = copy.deepcopy(VERIFICATION_403)
            else:
                ref = auto_schema.resolve_serializer(StapelErrorSerializer, "response").ref
                responses[key] = {
                    "content": {"application/json": {"schema": ref}},
                    "description": _status_phrase(status),
                }
        responses[key][ERRORS_KEY] = codes

    captcha = captcha_contract(view, method, request_serializer=request_serializer)
    if captcha:
        operation[CAPTCHA_KEY] = captcha
    return operation


def _status_phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""
