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

Token transmission — four channels, tried in this order
------------------------------------------------------
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
3. ``?token=<jwt>`` query parameter — the simplest explicit fallback. Query
   strings routinely land in proxy/server access logs.
4. The **JWT cookie** (0.44.1) — ``JWT_COOKIE_NAME``, and
   ``JWT_REFRESH_COOKIE_NAME`` where the deployment allows refresh. Names
   resolve through :func:`stapel_core.django.jwt.utils.jwt_cookie_names`, the
   same call the HTTP extractor makes, so the socket can never read a
   different cookie than the one HTTP sets.

**Channel 4 is why this module was useless in production for months.** A
browser cannot set an ``Authorization`` header on ``new WebSocket()``. The
product authenticates HTTP with an httpOnly JWT cookie — which the browser
*does* attach to the handshake — and this extractor had no cookie branch, so
every real browser handshake closed 4401, the client read 4401 as a permanent
refusal and stopped retrying, and the product fell back to polling. The socket
was built, mounted, proxied and smoke-tested; the smoke test sent an
``Authorization`` header a browser can never send, so it proved nothing about
the only path that matters.

The cookie is **last**, not first: an explicit credential always wins. A client
that deliberately hands over a subprotocol token gets that token honoured (and
skips the origin gate below); only a browser with nothing but its cookie falls
through to channel 4.

The origin gate — mandatory for the cookie, and only for the cookie
-------------------------------------------------------------------
A cookie is **ambient authority**. The browser attaches it to a WebSocket
handshake started by any page on the internet, and WebSockets are protected by
neither the same-origin policy nor CORS: there is no preflight, and the
cross-site handshake succeeds without the attacker's page ever reading the
cookie. Reading the cookie without checking ``Origin`` would have turned the
fix into Cross-Site WebSocket Hijacking, so the two ship together.

A cookie-authenticated handshake is admitted only when the ``Origin`` header is
present and on the allowlist resolved by
:mod:`stapel_core.django.jwt.ws_origin`. It **fails closed**: no allowlist
declared means every cookie handshake is refused with close code 4403. An
empty allowlist is a misconfiguration, not a wildcard, and
``stapel_core.jwt.E001`` says so at ``manage.py check`` time.

Channels 1-3 are **not** gated by origin. They are not ambient: an attacker's
page cannot produce a header, a subprotocol or a query token it has never
seen, so the ``Origin`` adds nothing there — while requiring one would refuse
every service-to-service and native client, which legitimately send none.

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
from http.cookies import SimpleCookie
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

# WebSocket application close code for "authenticated, but this handshake is
# not allowed" — the realtime analogue of HTTP 403. Used for the origin gate:
# the credential may well be valid, the *page* that opened the socket is not
# one this deployment serves. Kept distinct from 4401 so an operator reading a
# close code can tell a rejected credential from a rejected origin, and so a
# client does not read a misconfiguration as "your token is bad".
CLOSE_CODE_FORBIDDEN = 4403

#: Credential channels that a cross-site page cannot produce. Only the cookie
#: is ambient, so only the cookie is gated on Origin.
SOURCE_HEADER = "header"
SOURCE_SUBPROTOCOL = "subprotocol"
SOURCE_QUERY = "query"
SOURCE_COOKIE = "cookie"
AMBIENT_SOURCES = frozenset({SOURCE_COOKIE})

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


def _header(scope, name: bytes) -> str | None:
    """The first value of a raw ASGI header, decoded, or ``None``."""
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def _cookies_from_scope(scope) -> dict:
    """Parse the handshake ``Cookie`` header into a name -> value dict.

    A malformed cookie header yields whatever parsed; it never raises. The
    browser is the only thing that writes this header and it is not the
    client's job to be well-formed for us to refuse it safely.
    """
    raw = _header(scope, b"cookie")
    if not raw:
        return {}
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # pragma: no cover - SimpleCookie is forgiving
        return {}
    return {key: morsel.value for key, morsel in jar.items()}


def _token_from_cookies(scope) -> tuple[str | None, str | None]:
    """``(access_token, refresh_token)`` from the handshake cookies.

    Cookie names come from :func:`stapel_core.django.jwt.utils.jwt_cookie_names`
    — the same resolution the HTTP extractor uses — so the socket cannot end
    up reading a cookie the HTTP side never sets. Django settings may be
    unavailable (this extractor is also called from other packages' system
    checks as a behavioural probe), in which case there are no names to read
    and the answer is simply "no cookie credential".
    """
    cookies = _cookies_from_scope(scope)
    if not cookies:
        return None, None
    try:
        from .utils import jwt_cookie_names

        access_name, refresh_name = jwt_cookie_names()
    except Exception:  # pragma: no cover - settings not configured
        access_name, refresh_name = "stapel_jwt", "stapel_refresh_jwt"
    return cookies.get(access_name) or None, cookies.get(refresh_name) or None


def _extract_credential(scope) -> tuple[str | None, str | None, str | None]:
    """``(token, refresh_token, source)`` for this handshake.

    Precedence: Authorization header -> Sec-WebSocket-Protocol subprotocol ->
    ?token= query parameter -> JWT cookie. ``source`` names the channel the
    credential arrived on, because the caller must gate the ambient one
    (cookie) on ``Origin`` and must not gate the others. See the module
    docstring.

    ``refresh_token`` is non-``None`` only on the cookie path: it is the one
    channel where the client did not choose what to send, so it is the one
    channel where a browser can be holding a live refresh cookie behind an
    expired access cookie.
    """
    # 1. Authorization: Bearer <token>
    header = _header(scope, b"authorization")
    if header and header[:7].lower() == "bearer ":
        token = header[7:].strip()
        if token:
            return token, None, SOURCE_HEADER

    # 2. Sec-WebSocket-Protocol subprotocol
    token = _token_from_subprotocols(_subprotocols_from_scope(scope))
    if token:
        return token, None, SOURCE_SUBPROTOCOL

    # 3. ?token=<jwt> query parameter
    query_string = scope.get("query_string") or b""
    if query_string:
        params = parse_qs(query_string.decode("latin-1"))
        values = params.get("token")
        if values and values[0]:
            return values[0], None, SOURCE_QUERY

    # 4. The JWT cookie — the only channel a browser gets for free, and the
    #    only ambient one. A refresh cookie alone is still a credential: the
    #    access cookie expires first, and refusing there is the 4401 that
    #    taught the client to stop retrying.
    access, refresh = _token_from_cookies(scope)
    if access or refresh:
        return access, refresh, SOURCE_COOKIE

    return None, None, None


def _extract_token(scope) -> str | None:
    """The token alone, for callers that only need "can this be read?".

    Kept as the stable probe surface: other packages' system checks call it
    with a synthetic scope to ask, behaviourally, which credential channels
    this version of core understands.
    """
    return _extract_credential(scope)[0]


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


def _authenticate_cookie(token: str | None, refresh_token: str | None):
    """Cookie-path authentication: access cookie, then the refresh cookie.

    Mirrors the HTTP middleware's step 6. An access cookie has a lifetime
    (one hour by default) and the browser holds a refresh cookie for days
    behind it, so a tab left open past expiry reconnects with a stale access
    cookie and a live refresh cookie — exactly the state in which a 4401
    teaches the client that the socket is permanently refused.

    Gated on ``JWT_REFRESH_ALLOWED``, the same flag the HTTP middleware reads,
    so a consumer-mode service that must not re-mint still will not. The
    re-mint goes through ``load_user_by_uid``, so a deactivated, deleted or
    tombstoned uid is refused here as it is on HTTP — never from the refresh
    token's own (up to 7-day stale) claims.

    Returns ``(user, claims, new_access_token_or_None)``. The third element is
    stamped into the scope: a handshake has no response to set a cookie on, so
    the host is the only thing that can hand the fresh token back to the
    client.
    """
    if token:
        user, claims = _authenticate_token(token)
        if user is not None and claims is not None:
            return user, claims, None

    if not refresh_token:
        return None, None, None

    from django.conf import settings

    if not getattr(settings, "JWT_REFRESH_ALLOWED", False):
        return None, None, None

    from .provider import jwt_provider
    from .utils import load_user_by_uid

    new_token = jwt_provider.refresh_access_token(refresh_token, load_user_by_uid)
    if not new_token:
        return None, None, None
    user, claims = _authenticate_token(new_token)
    if user is None or claims is None:
        return None, None, None
    return user, claims, new_token


class JWTAuthMiddleware:
    """ASGI middleware that authenticates WebSocket connections via Stapel JWT.

    Plain ASGI middleware (works anywhere in a Channels routing stack). For
    non-WebSocket scopes it is a transparent pass-through.

    :param allowed_origins: overrides the settings-resolved allowlist for the
        cookie path (mostly a test affordance). ``None`` means "resolve it from
        settings", which is what every host should do. Passing an empty list is
        not a wildcard — it is an empty allowlist, and cookie handshakes are
        refused, exactly as when nothing is configured.
    """

    def __init__(self, inner, allowed_origins=None):
        self.inner = inner
        self._allowed_origins = allowed_origins
        self._warned_unguarded = False

    async def __call__(self, scope, receive, send):
        # Only guard WebSocket handshakes; leave other protocols untouched.
        if scope.get("type") != "websocket":
            return await self.inner(scope, receive, send)

        # Copy so we never mutate a scope shared with sibling middleware.
        scope = dict(scope)

        token, refresh_token, source = _extract_credential(scope)

        # A cookie is ambient authority: the browser attached it without the
        # page asking, so the page must be one we serve. This runs BEFORE the
        # token is validated — an unlisted origin never gets to learn whether
        # the victim's cookie is currently valid.
        if source in AMBIENT_SOURCES and not self._origin_permitted(scope):
            await self._deny(receive, send, code=CLOSE_CODE_FORBIDDEN)
            return

        user = None
        claims = None
        refreshed = None
        if token or refresh_token:
            try:
                if source == SOURCE_COOKIE:
                    user, claims, refreshed = await database_sync_to_async(
                        _authenticate_cookie
                    )(token, refresh_token)
                else:
                    user, claims = await database_sync_to_async(
                        _authenticate_token
                    )(token)
            except Exception:
                # Never let an auth error surface as a logged exception — a
                # flood of bad tokens must not spam the error log. Reject
                # quietly (DEBUG) just like an invalid token.
                logger.debug("Channels JWT authentication failed")
                user, claims, refreshed = None, None, None

        if user is None or claims is None:
            await self._deny(receive, send)
            return

        scope["user"] = user
        scope["stapel_claims"] = claims
        scope["stapel_auth_source"] = source
        if refreshed:
            # The handshake has no response to set a cookie on; the host hands
            # this to the client (or ignores it) rather than the browser
            # silently keeping a stale cookie forever.
            scope["stapel_refreshed_access_token"] = refreshed
        return await self.inner(scope, receive, send)

    def _origin_permitted(self, scope) -> bool:
        """Is this cookie handshake's ``Origin`` one this deployment serves?

        Fails closed on every uncertain answer: no allowlist configured, no
        ``Origin`` header, an unparseable one, or one that is simply not
        listed. Browsers always send ``Origin`` on a WebSocket handshake, so
        "absent" here means a client that could just as well have sent a
        non-ambient credential instead.
        """
        from .ws_origin import origin_allowed, websocket_origin_allowlist

        allowed = websocket_origin_allowlist(self._allowed_origins)
        if not allowed:
            # A misconfiguration, not an attack: say so once per process at
            # WARNING (the boot check stapel_core.jwt.E001 already said it),
            # then stay quiet so a cross-site flood cannot spam the log.
            if not self._warned_unguarded:
                self._warned_unguarded = True
                logger.warning(
                    "Channels JWT: refusing cookie-authenticated WebSocket "
                    "handshakes because no origin allowlist is configured. "
                    "Set STAPEL_WS_ALLOWED_ORIGINS (see stapel_core.jwt.E001)."
                )
            return False

        origin = _header(scope, b"origin")
        if origin_allowed(origin, allowed):
            return True
        logger.info("Channels JWT: refused websocket origin %r", origin)
        return False

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


def JWTAuthMiddlewareStack(inner, allowed_origins=None):
    """Convenience factory mirroring Channels' ``AuthMiddlewareStack``.

    JWT auth is self-contained (no cookie/session middleware needed), so the
    stack is just the JWT middleware. Provided for call-site symmetry with the
    Channels idiom. ``allowed_origins`` is forwarded to the origin gate.
    """
    return JWTAuthMiddleware(inner, allowed_origins=allowed_origins)
