"""Function primitive — synchronous name-addressed call with a result."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .config import comm_setting
from .exceptions import (
    FunctionCallError,
    FunctionNotRegistered,
    FunctionRouteNotConfigured,
)
from .registry import FunctionHandler, function_registry

logger = logging.getLogger(__name__)

_session = None
_session_lock = threading.Lock()
_custom_transport = None
_custom_transport_path = None


def register_function(name: str, handler: FunctionHandler, *, schema: dict | None = None) -> None:
    function_registry.register(name, handler, schema=schema)


def function(name: str, *, schema: dict | None = None) -> Callable[[FunctionHandler], FunctionHandler]:
    """Decorator: register *name*'s single provider.

        @function("cdn.media_exists")
        def media_exists(payload: dict) -> dict: ...
    """

    def decorator(handler: FunctionHandler) -> FunctionHandler:
        register_function(name, handler, schema=schema)
        return handler

    return decorator


def resolve_timeout(name: str, timeout: float | None) -> float:
    """Seconds :func:`call` should give *name*: explicit, then named, then global.

    An explicit argument is the caller's last word — a settings map must not
    override a number a call site chose deliberately. Otherwise the
    ``FUNCTION_TIMEOUTS`` entry with the longest matching prefix wins, so a
    deployment can say "everything this module does is slow" in one line and
    still single out one Function inside it.
    """
    if timeout is not None:
        return float(timeout)
    overrides = comm_setting("FUNCTION_TIMEOUTS", {}) or {}
    best_key = ""
    for key in overrides:
        if name == key or name.startswith(key):
            if len(key) > len(best_key):
                best_key = key
    if best_key:
        return float(overrides[best_key])
    return float(comm_setting("FUNCTION_TIMEOUT", 5.0))


def call(name: str, payload: dict | None = None, *, timeout: float | None = None) -> Any:
    """Invoke function *name* and return its result.

    Raises FunctionNotRegistered / FunctionRouteNotConfigured on wiring
    errors and FunctionCallError when the provider fails. Callers decide
    whether a failure is fatal — never swallow it into a fail-open default
    on security-relevant paths.
    """
    payload = payload or {}
    function_registry.validate(name, payload)

    transport = comm_setting("FUNCTION_TRANSPORT", "inprocess")
    if transport == "inprocess":
        handler = function_registry.get(name)
        try:
            return handler(payload)
        except Exception as exc:
            raise FunctionCallError(f"function '{name}' failed: {exc!r}") from exc

    # Resolved once, here, so every remote transport is bound by the same
    # number and a deployment cannot end up with one answer over nats and
    # another over http. In-process is untouched: there is no wire to bound.
    effective = resolve_timeout(name, timeout)

    if transport == "nats":
        from .nats import nats_function_transport

        return nats_function_transport(name, payload, timeout=effective)

    if transport == "http":
        return _call_http(name, payload, timeout=effective)

    # Custom transport (gRPC, ...): a dotted path to a callable
    # ``transport(name, payload, timeout=None) -> Any``. Lets a deployment
    # swap the RPC mechanism without touching module code.
    if "." in transport:
        return _custom_call(transport, name, payload, timeout=effective)

    raise FunctionRouteNotConfigured(
        f"unknown FUNCTION_TRANSPORT {transport!r} "
        "(expected 'inprocess', 'nats', 'http', or a dotted path to a transport callable)"
    )


def _custom_call(dotted: str, name: str, payload: dict, *, timeout: float | None) -> Any:
    global _custom_transport, _custom_transport_path
    if _custom_transport is None or _custom_transport_path != dotted:
        from django.utils.module_loading import import_string

        with _session_lock:
            _custom_transport = import_string(dotted)
            _custom_transport_path = dotted
    return _custom_transport(name, payload, timeout=timeout)


def function_unreachable_reason(name: str) -> str | None:
    """Why :func:`call` cannot reach function *name* here, or None if it can.

    "Is this seam wired" is a question about the transport this deployment
    runs, and each transport addresses a function differently. This asks the
    same question :func:`call` answers, branch for branch:

    * ``inprocess`` — ``call()`` looks the name up in the process-local
      registry, so a provider must be registered in this process;
    * ``http`` — ``call()`` resolves a longest-prefix ``FUNCTION_ROUTES``
      entry and ignores the registry entirely, so only a matching route makes
      the function reachable — even in a process that also provides it;
    * ``nats`` — the subject IS the function name and there is no route table
      (``comm/nats.py``); a deployment whose provider runs
      ``manage.py serve_functions`` is wired, and nothing here can (or should)
      prove that provider is up. That is what the runtime timeout is for;
    * a dotted path — a custom transport does its own addressing.

    Anything else is a transport ``call()`` cannot dispatch at all: it raises
    ``FunctionRouteNotConfigured`` on every call, so the seam is as
    unreachable as an unwired one.

    Never a liveness probe: it reads settings and the registry, nothing else.
    Whether the transport's client library is importable is a separate
    question, answered by ``stapel_preflight``'s transport-dependency check.
    """
    from .registry import function_registry

    transport = str(comm_setting("FUNCTION_TRANSPORT", "inprocess") or "")
    if transport == "inprocess":
        try:
            function_registry.get(name)
        except FunctionNotRegistered:
            return (
                f"the transport is 'inprocess' and no provider for {name} is "
                "registered in this process"
            )
        return None
    if transport == "http":
        try:
            _route_for(name)
        except FunctionRouteNotConfigured:
            return (
                "the transport is 'http' and no "
                f"STAPEL_COMM['FUNCTION_ROUTES'] prefix matches {name}"
            )
        return None
    if transport == "nats" or "." in transport:
        return None
    return (
        f"STAPEL_COMM['FUNCTION_TRANSPORT'] is {transport!r}, which is not a "
        "transport comm can dispatch on ('inprocess', 'nats', 'http', or a "
        "dotted path to a transport callable)"
    )


def _route_for(name: str) -> str:
    routes: dict[str, str] = comm_setting("FUNCTION_ROUTES", {}) or {}
    best = ""
    for prefix in routes:
        if name.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    if not best:
        raise FunctionRouteNotConfigured(
            f"no FUNCTION_ROUTES entry matches function '{name}'"
        )
    return routes[best]


def _http_session():
    """Shared pooled session.

    A bare ``requests.post`` opens (and half-closes) a fresh TCP connection
    per call; under a busy caller that exhausts the ephemeral-port range and
    the client starts failing intermittently. A module-wide Session with an
    explicitly sized pool keeps connections alive and bounds concurrency.

    Retries cover CONNECT failures only — a Function call is not guaranteed
    idempotent, so a request that may already have reached the provider is
    never resent automatically.
    """
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                import requests
                from requests.adapters import HTTPAdapter

                try:
                    from urllib3.util.retry import Retry

                    retries = Retry(
                        total=None,
                        connect=int(comm_setting("HTTP_CONNECT_RETRIES", 2)),
                        read=0,
                        status=0,
                        backoff_factor=0.1,
                    )
                except ImportError:  # pragma: no cover
                    retries = 0

                adapter = HTTPAdapter(
                    pool_connections=int(comm_setting("HTTP_POOL_CONNECTIONS", 10)),
                    pool_maxsize=int(comm_setting("HTTP_POOL_MAXSIZE", 50)),
                    max_retries=retries,
                )
                session = requests.Session()
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                _session = session
    return _session


def reset_http_session() -> None:
    """Tests / settings-change hook."""
    global _session
    with _session_lock:
        _session = None


def _call_http(name: str, payload: dict, *, timeout: float | None) -> Any:
    import requests
    from django.conf import settings

    base = _route_for(name).rstrip("/")
    url = f"{base}/api/_functions/{name}/"
    headers = {}
    api_key = getattr(settings, "SERVICE_API_KEY", None)
    if api_key:
        headers["X-API-KEY"] = api_key

    try:
        resp = _http_session().post(
            url,
            json={"payload": payload},
            headers=headers,
            timeout=timeout or comm_setting("FUNCTION_TIMEOUT", 5.0),
        )
    except requests.RequestException as exc:
        raise FunctionCallError(f"function '{name}' unreachable at {url}: {exc!r}") from exc

    if resp.status_code == 404:
        # "The service has no such function" and "this URL does not exist"
        # are different facts wearing the same status code, and callers act
        # on the difference: FunctionNotRegistered is a DEGRADE signal —
        # `require_capability` answers it by falling back to the builtin
        # role→capability table, where every client-defined custom role
        # denies. A mis-set FUNCTION_ROUTES would therefore silently
        # downgrade authorization instead of failing loudly.
        #
        # The remote function view (comm/http.py) renders its 404 as JSON;
        # Django's URL resolver and any proxy in front of it render HTML.
        # Same distinction stapel-core/django/workspaces.py draws, and for
        # the same reason (owner-reported live incident, 2026-07-26: a
        # routing 404 read as a verdict told a workspace owner they were not
        # a member).
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type == "application/json":
            raise FunctionNotRegistered(f"remote service has no function '{name}' ({url})")
        raise FunctionCallError(
            f"function '{name}': {url} does not route to a function endpoint "
            f"(404 from the URL resolver, not the view). Check FUNCTION_ROUTES "
            f"and that the remote service mounts get_function_urls()."
        )
    if resp.status_code >= 400:
        raise FunctionCallError(
            f"function '{name}' returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise FunctionCallError(f"function '{name}' failed remotely: {data['error']}")
    return data.get("result") if isinstance(data, dict) else data
