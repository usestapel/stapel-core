"""
Django Channels authentication middleware for Stapel JWT.

This is the WebSocket/ASGI counterpart of the HTTP JWT stack
(:mod:`stapel_core.django.jwt.middleware` /
:mod:`stapel_core.django.jwt.authentication`). It validates a token with the
**same** :data:`stapel_core.django.jwt.provider.jwt_provider` used by HTTP —
same signing config, same token-level and user-level blacklists, same
``get_or_create_user_from_jwt`` user sync — so a token that authenticates an
HTTP request authenticates a WebSocket identically.

On success the connection scope is populated exactly like HTTP populates the
request:

* ``scope["user"]``          — the Django ``User`` (mirrors ``request.user``),
                               already carrying the transient staff-roles claim
                               stamped by ``get_or_create_user_from_jwt``.
* ``scope["stapel_claims"]`` — the validated token payload dict (the same dict
                               HTTP auth derives ``request.user`` from).

On failure — missing, malformed, expired, blacklisted token, unknown/banned
user, or any error during validation — the connection is **rejected before
``websocket.accept``** by replying to the handshake with
``websocket.close`` and application close code **4401** (the WebSocket analogue
of HTTP 401 Unauthorized, in the private-use 4000–4999 range). Rejection is
silent: failures are logged at DEBUG only, never as exceptions/errors, so a
flood of unauthenticated connection attempts cannot spam the error log.

Token transmission — two supported conventions, tried in this order
------------------------------------------------------------------
Browsers cannot set custom headers on the WebSocket handshake, so the two
browser-usable channels are the ``Sec-WebSocket-Protocol`` subprotocol and the
query string. Precedence (first match wins):

1. ``Authorization: Bearer <token>`` request header — for non-browser clients
   (service-to-service, tests, native apps) that can set headers. Preferred
   because headers are not written to WebSocket URLs / server access logs.
2. ``Sec-WebSocket-Protocol`` subprotocol — the browser-friendly, log-safe
   channel. Two shapes are accepted:
     * a single ``"<scheme>.<token>"`` value, e.g. ``"bearer.<jwt>"``; or
     * a ``["<scheme>", "<token>"]`` pair, e.g. ``new WebSocket(url,
       ["bearer", token])``.
   Recognized schemes: ``authorization``, ``bearer``, ``access_token``,
   ``jwt``, ``token``.
3. ``?token=<jwt>`` query parameter — the simplest browser fallback. Least
   preferred: query strings routinely land in proxy/server access logs.

Optional dependency
-------------------
Channels is an **optional** extra. This submodule is never imported by the
package on a normal (HTTP-only) Django start — nothing in ``stapel_core`` or
``stapel_core.django`` imports it — so services that don't do realtime pay
nothing. Importing it without ``channels`` installed raises a clear
``ImportError`` telling you to ``pip install 'stapel-core[channels]'``.

Usage (asgi.py)::

    from channels.routing import ProtocolTypeRouter, URLRouter
    from stapel_core.django.jwt.channels import JWTAuthMiddlewareStack
    from myapp.routing import websocket_urlpatterns

    application = ProtocolTypeRouter({
        "http": django_asgi_app,
        "websocket": JWTAuthMiddlewareStack(
            URLRouter(websocket_urlpatterns)
        ),
    })

Then in a consumer, ``self.scope["user"]`` and
``self.scope["stapel_claims"]`` are populated; unauthenticated clients never
reach the consumer (the socket is closed with 4401 during the handshake).
"""

import logging
from urllib.parse import parse_qs

# Channels is an optional dependency. Importing this submodule without it must
# fail loudly and helpfully rather than with a bare "No module named channels".
try:
    from channels.db import database_sync_to_async
except ImportError as exc:  # pragma: no cover - exercised via sys.modules stub
    raise ImportError(
        "stapel_core.django.jwt.channels requires the optional 'channels' "
        "dependency, which is not installed. Install it with:\n"
        "    pip install 'stapel-core[channels]'"
    ) from exc

logger = logging.getLogger(__name__)

# WebSocket application close code for "unauthorized" — the realtime analogue
# of HTTP 401. 4401 is in the private-use range (4000–4999) reserved for
# application-defined codes and mirrors the 401 status for easy correlation.
CLOSE_CODE_UNAUTHORIZED = 4401

# Subprotocol scheme names understood as "the next value / the dotted suffix is
# the token".
_SUBPROTOCOL_SCHEMES = frozenset(
    {"authorization", "bearer", "access_token", "jwt", "token"}
)


def _subprotocols_from_scope(scope) -> list:
    """Return the advertised WebSocket subprotocols as a list of strings.

    Prefers the ASGI ``scope["subprotocols"]`` list; falls back to parsing the
    raw ``Sec-WebSocket-Protocol`` header (comma-separated) if that key is
    absent.
    """
    protocols = scope.get("subprotocols")
    if protocols:
        return [str(p).strip() for p in protocols if str(p).strip()]

    for name, value in scope.get("headers") or ():
        if name == b"sec-websocket-protocol":
            raw = value.decode("latin-1")
            return [p.strip() for p in raw.split(",") if p.strip()]
    return []


def _token_from_subprotocols(protocols) -> str | None:
    """Extract a bearer token from advertised subprotocols.

    Accepts either ``"<scheme>.<token>"`` (single value; split on the FIRST dot
    so the JWT's own dots are preserved) or a ``["<scheme>", "<token>"]`` pair.
    """
    # Shape 1: "<scheme>.<token>"
    for proto in protocols:
        if "." in proto:
            scheme, _, token = proto.partition(".")
            if scheme.lower() in _SUBPROTOCOL_SCHEMES and token:
                return token
    # Shape 2: ["<scheme>", "<token>"]
    for index, proto in enumerate(protocols):
        if proto.lower() in _SUBPROTOCOL_SCHEMES and index + 1 < len(protocols):
            following = protocols[index + 1]
            if following:
                return following
    return None


def _extract_token(scope) -> str | None:
    """Pull the JWT out of the connection scope.

    Precedence: Authorization header -> Sec-WebSocket-Protocol subprotocol ->
    ?token= query parameter. See module docstring for the rationale.
    """
    # 1. Authorization: Bearer <token>
    for name, value in scope.get("headers") or ():
        if name == b"authorization":
            header = value.decode("latin-1")
            if header[:7].lower() == "bearer ":
                token = header[7:].strip()
                if token:
                    return token

    # 2. Sec-WebSocket-Protocol subprotocol
    token = _token_from_subprotocols(_subprotocols_from_scope(scope))
    if token:
        return token

    # 3. ?token=<jwt> query parameter
    query_string = scope.get("query_string") or b""
    if query_string:
        params = parse_qs(query_string.decode("latin-1"))
        values = params.get("token")
        if values and values[0]:
            return values[0]

    return None


def _authenticate_token(token: str):
    """Validate a token and resolve the Django user, mirroring HTTP auth.

    Runs the identical sequence the HTTP path does
    (``middleware.JWTAuthMiddleware._authenticate`` /
    ``authentication.JWTCookieAuthentication.authenticate``): token-level
    blacklist, signature/claims validation, user-level blacklist, then
    ``get_or_create_user_from_jwt`` (which also stamps the transient
    staff-roles claim used by ``stapel_core.access``).

    Returns ``(user, claims)`` on success, ``(None, None)`` otherwise. Runs in
    a thread via ``database_sync_to_async`` (it touches the cache and the ORM).
    """
    from .provider import jwt_provider
    from .authentication import is_user_blacklisted
    from .utils import get_or_create_user_from_jwt

    if jwt_provider.is_blacklisted(token):
        return None, None

    claims = jwt_provider.validate_token(token)
    if not claims:
        return None, None

    user_id = claims.get("user_id")
    if user_id and is_user_blacklisted(user_id):
        return None, None

    user = get_or_create_user_from_jwt(claims)
    if not user:
        return None, None

    return user, claims


class JWTAuthMiddleware:
    """ASGI middleware that authenticates WebSocket connections via Stapel JWT.

    Plain ASGI middleware (works anywhere in a Channels routing stack). For
    non-WebSocket scopes it is a transparent pass-through.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        # Only guard WebSocket handshakes; leave other protocols untouched.
        if scope.get("type") != "websocket":
            return await self.inner(scope, receive, send)

        # Copy so we never mutate a scope shared with sibling middleware.
        scope = dict(scope)

        user = None
        claims = None
        token = _extract_token(scope)
        if token:
            try:
                user, claims = await database_sync_to_async(_authenticate_token)(
                    token
                )
            except Exception:
                # Never let an auth error surface as a logged exception — a
                # flood of bad tokens must not spam the error log. Reject
                # quietly (DEBUG) just like an invalid token.
                logger.debug("Channels JWT authentication failed")
                user, claims = None, None

        if user is None or claims is None:
            await self._deny(receive, send)
            return

        scope["user"] = user
        scope["stapel_claims"] = claims
        return await self.inner(scope, receive, send)

    async def _deny(self, receive, send, code: int = CLOSE_CODE_UNAUTHORIZED):
        """Reject the handshake before accept with the given close code.

        Drains the initial ``websocket.connect`` so the ``websocket.close`` is a
        valid handshake reply, then closes. The consumer is never invoked.
        """
        try:
            await receive()  # the initial websocket.connect
        except Exception:
            # If the transport is already gone, closing is moot.
            pass
        await send({"type": "websocket.close", "code": code})


def JWTAuthMiddlewareStack(inner):
    """Convenience factory mirroring Channels' ``AuthMiddlewareStack``.

    JWT auth is self-contained (no cookie/session middleware needed), so the
    stack is just the JWT middleware. Provided for call-site symmetry with the
    Channels idiom.
    """
    return JWTAuthMiddleware(inner)
