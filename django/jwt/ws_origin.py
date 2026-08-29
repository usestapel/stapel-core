"""The WebSocket origin allowlist, and the boot gate that insists on one.

Why this module exists
----------------------
0.44.1 gave the Channels handshake extractor a **cookie** branch, because a
browser cannot set an ``Authorization`` header on ``new WebSocket()`` and the
product authenticates HTTP with an httpOnly JWT cookie — so every real browser
handshake used to close 4401 and the client fell back to polling forever.

Reading the cookie fixes that and, on its own, opens a worse hole. A cookie is
**ambient authority**: the browser attaches it to a WebSocket handshake started
by *any* page on the internet, and WebSockets are protected by neither the
same-origin policy nor CORS — there is no preflight, and a cross-site
handshake succeeds without the attacker's page ever being able to read the
cookie. That is Cross-Site WebSocket Hijacking, and the only thing standing
between a cookie-authenticated socket and it is the ``Origin`` header.

So the cookie branch and this module ship together, and the guard **fails
closed**: a cookie-authenticated handshake is refused when no allowlist is
declared. An empty allowlist is a misconfiguration, not a wildcard. The
credential channels that are *not* ambient — ``Authorization``, the
``Sec-WebSocket-Protocol`` subprotocol, ``?token=`` — are not gated by origin,
because an attacker's page cannot produce one: it never sees the victim's
token, and requiring an ``Origin`` there would break every service-to-service
and native client, which legitimately send none.

Where the allowlist comes from
------------------------------
1. ``STAPEL_WS_ALLOWED_ORIGINS`` — the core setting, canonical for any host
   that mounts :mod:`stapel_core.django.jwt.channels`.
2. ``STAPEL_REALTIME["ALLOWED_ORIGINS"]`` — read as a plain settings dict, with
   no import of ``stapel_realtime`` (core does not depend on it). A deployment
   already running the realtime substrate has declared its origins there; it
   must not have to declare them twice, and two lists that can disagree is how
   a guard ends up meaning something different at each layer. Read only when
   the core setting is empty.
3. ``STAPEL_SITES`` (:mod:`stapel_core.sites`) — **added to** whichever of the
   two above applies, never replacing it. Every host and alias in the site
   registry is an origin this deployment serves by definition, and a
   multi-brand deployment must not have to list the same hostnames a third
   time. The extra entries a socket needs and no page is served from (a Vite
   dev server, a native shell) stay in the setting.

Coordination with ``stapel_chat.E014``
--------------------------------------
stapel-chat 0.4.0 ships ``stapel_chat.E014`` for exactly this fact at its own
layer. Because both read the same allowlist, the two verdicts agree by
construction — chat cannot say "guarded" while core says "unguarded". The
mechanism belongs here, so every consumer of the core socket inherits it
rather than each module re-deriving it: a chat module's check cannot protect a
video socket. Consumers should delegate to :func:`websocket_origin_allowlist`
and :func:`cookie_websocket_auth_reachable` rather than re-reading settings.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from django.core import checks

from stapel_core.django.check_guard import (
    SecurityCriticalError,
    declare_security_critical,
)

logger = logging.getLogger(__name__)

#: The id IS its security-critical declaration, so no blanket
#: SILENCED_SYSTEM_CHECKS line can mute it (stapel_core.django.check_guard).
E001_WS_COOKIE_AUTH_UNGUARDED = declare_security_critical(
    "stapel_core.jwt.E001",
    "a cookie-authenticated WebSocket with no origin allowlist is "
    "cross-site WebSocket hijacking: the browser attaches the credential to a "
    "handshake started by any page on the internet",
)

#: An entry that can never match an incoming Origin. Not security-critical on
#: its own — the guard drops it and keeps refusing — but a guard that silently
#: never matches is the other half of the same incident.
E002_MALFORMED_ORIGIN = "stapel_core.jwt.E002"

#: Ports that are implicit in an origin of the matching scheme.
_DEFAULT_PORTS = {"http": "80", "https": "443", "ws": "80", "wss": "443"}

#: A browser's ``Origin`` header is always http(s) — it never says ``wss``.
#: An allowlist written in socket schemes is a door that silently never opens,
#: so the socket schemes fold onto the HTTP ones they are served over.
_SCHEME_ALIASES = {"ws": "http", "wss": "https"}


def normalize_origin(origin: str) -> str:
    """``WSS://App.Example.COM:443/x`` -> ``https://app.example.com``.

    Case and a default port are noise; a non-default port is identity — an
    allowlist entry of ``studio.localhost`` that never matched
    ``http://studio.localhost:8600`` is a real incident, not a hypothetical.
    ``ws``/``wss`` fold onto ``http``/``https`` so an allowlist written in
    socket schemes still matches the ``Origin`` a browser actually sends.

    :raises ValueError: the string is not a ``scheme://host[:port]`` origin.
    """
    parts = urlsplit(str(origin).strip())
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        raise ValueError(f"{origin!r} is not a scheme://host[:port] origin")
    port = parts.port
    default_port = _DEFAULT_PORTS.get(scheme)
    scheme = _SCHEME_ALIASES.get(scheme, scheme)
    if port is None or str(port) == default_port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _site_origins() -> list:
    """``https://<host>`` for every host and alias in ``STAPEL_SITES``.

    A registered site IS an origin this deployment serves, so its sockets must
    open without the operator writing the same hostnames a third time (after
    ``ALLOWED_HOSTS`` and ``CSRF_TRUSTED_ORIGINS``, both already derived from
    the registry). Never a wildcard and never a widening: a malformed registry
    contributes nothing and is reported by ``stapel_core.sites.E001``.
    """
    from stapel_core.sites import SitesConfigError, registry_from_settings

    try:
        return list(registry_from_settings().origins())
    except SitesConfigError:
        return []


def configured_origins() -> list:
    """The raw allowlist entries, in resolution order. See the module doc."""
    from django.conf import settings

    raw = getattr(settings, "STAPEL_WS_ALLOWED_ORIGINS", None)
    if not raw:
        realtime = getattr(settings, "STAPEL_REALTIME", None) or {}
        try:
            raw = realtime.get("ALLOWED_ORIGINS") or []
        except AttributeError:  # not a mapping; reported by the realtime checks
            raw = []

    # Union, not "first non-empty wins": the site registry and the explicit
    # setting answer different questions ("which hosts do we serve" vs "which
    # extra origins may open a socket" — a dev server on :5173, a native shell)
    # and a deployment that declares both means both. Fail-closed is untouched:
    # with an empty registry AND an empty setting the allowlist stays empty and
    # cookie handshakes are still refused.
    entries: list = []
    for entry in list(raw) + _site_origins():
        if entry not in entries:  # `in`, not a set: an entry may be unhashable
            entries.append(entry)
    return entries


def websocket_origin_allowlist(entries=None) -> set:
    """The normalized allowlist. Malformed entries are dropped, not honoured.

    Dropping rather than honouring matters: a typo must not be the thing that
    decides a deployment does not get a guard. ``configured_origins()`` stays
    non-empty in that case, so the guard still refuses everything and
    ``stapel_core.jwt.E002`` names the entry.

    :param entries: an explicit list to normalize instead of reading settings.
        An empty list normalizes to an empty allowlist — never a wildcard.
    """
    raw = configured_origins() if entries is None else list(entries)
    allowed = set()
    for entry in raw:
        try:
            allowed.add(normalize_origin(entry))
        except (ValueError, AttributeError, TypeError):
            logger.warning("stapel-core: ignoring malformed allowed origin %r", entry)
    return allowed


def origin_allowed(origin, allowed=None) -> bool:
    """Is this handshake ``Origin`` on the allowlist?

    ``False`` for a missing origin, an unparseable one, and — always — for an
    empty allowlist. Callers use this only for ambient (cookie) credentials.

    :param allowed: a pre-normalized allowlist set; resolved from settings when
        omitted.
    """
    if allowed is None:
        allowed = websocket_origin_allowlist()
    if not allowed or not origin:
        return False
    try:
        return normalize_origin(origin) in allowed
    except (ValueError, AttributeError, TypeError):
        return False


def _serves_websockets() -> bool:
    """Can this process serve a WebSocket handshake at all?

    Provable at boot from the two things a Channels host must declare: an
    ``ASGI_APPLICATION``, or ``channels``/``stapel_realtime`` in
    ``INSTALLED_APPS``. An HTTP-only service answers False and never sees this
    check — the socket middleware is an optional extra it does not mount.
    """
    from django.conf import settings

    if getattr(settings, "ASGI_APPLICATION", None):
        return True
    installed = set(getattr(settings, "INSTALLED_APPS", ()) or ())
    return bool(installed & {"channels", "stapel_realtime"})


def _uses_cookie_credentials() -> bool:
    """Does a browser talking to this deployment hold a JWT cookie?

    Three independent tells, any one of which means yes: the DRF cookie
    authentication class is configured, the HTTP JWT middleware (whose
    extractor reads cookies first) is mounted, or this service mints and sets
    the cookies itself (``JWT_REFRESH_ALLOWED`` is the flag the middleware
    reads for `can manage cookies`).
    """
    from django.conf import settings

    rest = getattr(settings, "REST_FRAMEWORK", None) or {}
    classes = rest.get("DEFAULT_AUTHENTICATION_CLASSES") or ()
    if any("JWTCookie" in str(entry) for entry in classes):
        return True
    middleware = getattr(settings, "MIDDLEWARE", ()) or ()
    if any("stapel_core.django.jwt.middleware.JWTAuthMiddleware" == str(m)
           for m in middleware):
        return True
    return bool(getattr(settings, "JWT_REFRESH_ALLOWED", False))


def cookie_websocket_auth_reachable() -> bool:
    """Can a browser reach this deployment's socket carrying only a cookie?

    The precondition for :data:`E001_WS_COOKIE_AUTH_UNGUARDED`, and the
    predicate a consumer module should delegate to instead of re-deriving it.
    """
    return _serves_websockets() and _uses_cookie_credentials()


@checks.register("stapel_ws_origin")
def check_websocket_origin_allowlist(app_configs=None, **kwargs):
    """E001/E002 — the socket's origin guard, judged at deploy time.

    E-level, and not because the runtime is unsafe: the runtime fails closed,
    so an unguarded deployment refuses cookie handshakes rather than serving
    them. It is E-level because that refusal IS the shipped defect in its
    other form — a socket every browser is turned away from, a client that
    reads the close as permanent, and a product that quietly polls. The
    operator must learn this from ``manage.py check``, not from a support
    ticket months later.
    """
    if not cookie_websocket_auth_reachable():
        return []

    raw = configured_origins()
    if not raw:
        return [SecurityCriticalError(
            "This deployment serves WebSockets and authenticates browsers "
            "with the JWT cookie, but no WebSocket origin allowlist is "
            "declared. A cookie is ambient authority — the browser attaches "
            "it to a handshake started by any page on the internet, and "
            "WebSockets are protected by neither the same-origin policy nor "
            "CORS. stapel-core therefore REFUSES every cookie-authenticated "
            "handshake while the allowlist is empty (close 4403), which "
            "means this deployment's browser sockets do not work at all.",
            hint="Declare the origins this deployment is served from, WITH "
                 "their ports: STAPEL_WS_ALLOWED_ORIGINS = "
                 "['https://app.example.com', 'http://localhost:5173']. "
                 "STAPEL_REALTIME['ALLOWED_ORIGINS'] is read as well, so a "
                 "host already running stapel-realtime declares it once. "
                 "Clients that send the token as a subprotocol or ?token= "
                 "are not ambient and are not gated by this.",
            id=E001_WS_COOKIE_AUTH_UNGUARDED,
        )]

    findings = []
    for entry in raw:
        try:
            normalize_origin(entry)
        except (ValueError, AttributeError, TypeError):
            findings.append(checks.Error(
                f"WebSocket allowed origin {entry!r} is not a "
                "scheme://host[:port] origin, so it can never match an "
                "incoming Origin header. It is dropped from the allowlist.",
                hint="Write the full origin including scheme and, when it is "
                     "not the scheme's default, the port: "
                     "'http://studio.localhost:8600', not 'studio.localhost'.",
                id=E002_MALFORMED_ORIGIN,
            ))
    return findings


__all__ = [
    "E001_WS_COOKIE_AUTH_UNGUARDED",
    "E002_MALFORMED_ORIGIN",
    "configured_origins",
    "cookie_websocket_auth_reachable",
    "normalize_origin",
    "origin_allowed",
    "websocket_origin_allowlist",
]
