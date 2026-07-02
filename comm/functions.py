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

    if transport == "http":
        return _call_http(name, payload, timeout=timeout)

    # Custom transport (gRPC, NATS request-reply, ...): a dotted path to a
    # callable ``transport(name, payload, timeout=None) -> Any``. Lets a
    # deployment swap the RPC mechanism without touching module code.
    if "." in transport:
        return _custom_call(transport, name, payload, timeout=timeout)

    raise FunctionRouteNotConfigured(
        f"unknown FUNCTION_TRANSPORT {transport!r} "
        "(expected 'inprocess', 'http', or a dotted path to a transport callable)"
    )


def _custom_call(dotted: str, name: str, payload: dict, *, timeout: float | None) -> Any:
    global _custom_transport, _custom_transport_path
    if _custom_transport is None or _custom_transport_path != dotted:
        from django.utils.module_loading import import_string

        with _session_lock:
            _custom_transport = import_string(dotted)
            _custom_transport_path = dotted
    return _custom_transport(name, payload, timeout=timeout)


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
        raise FunctionNotRegistered(f"remote service has no function '{name}' ({url})")
    if resp.status_code >= 400:
        raise FunctionCallError(
            f"function '{name}' returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise FunctionCallError(f"function '{name}' failed remotely: {data['error']}")
    return data.get("result") if isinstance(data, dict) else data
