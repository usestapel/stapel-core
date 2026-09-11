"""
DRF authentication classes for Stapel services.

These classes integrate with the JWT middleware to provide authentication
for DRF views and Swagger documentation.
"""

import logging
from rest_framework import authentication

from stapel_core.core.drop import DropReport, drop_cache_key
from stapel_core.core.revocation_store import revocation_cache, revocation_namespace

logger = logging.getLogger(__name__)

# User-level blacklist key, written inside the SHARED revocation namespace
# (stapel_core.core.revocation_store) so one ban is visible to every service
# that verifies tokens signed by the same key.
#
# This used to reach for `cache.client.get_client()` — a raw django_redis
# handle — precisely to bypass Django's per-service cache KEY_PREFIX. That
# workaround was right about the problem and wrong about the scope: it worked
# on exactly one backend and silently fell back to the prefix-scoped (i.e.
# per-service, i.e. broken) path on every other, and it left the OTHER half
# of revocation — the per-jti TokenBlacklist — with no bypass at all. The
# namespace is now the mechanism, both halves use it, and no backend is
# special.
_USER_BLACKLIST_PREFIX = 'user_blacklisted:'


def _blacklist_fail_open() -> bool:
    """Honour the one blacklist escape hatch (shared with ``TokenBlacklist``).

    A deployment that has decided availability outranks revocation sets this
    once and both blacklists read it — a second knob would let the two halves
    of revocation drift apart.
    """
    from django.conf import settings
    return bool(getattr(settings, "STAPEL_BLACKLIST_FAIL_OPEN", False))


def blacklist_user(user_id: str, ttl: int = 7200) -> bool:
    """
    Blacklist a user so all their tokens are rejected.

    Written into the shared revocation namespace, so the ban is visible to
    every service pointed at the same store regardless of each one's own
    cache ``KEY_PREFIX`` — on every backend, not only django_redis.

    Args:
        user_id: UUID of the user to blacklist
        ttl: Time to live in seconds (default 2h, should be >= access token lifetime)

    Returns:
        True when the ban was stored, False when the store rejected it — a
        caller that ignores the result cannot tell a ban from a no-op.
    """
    key = f'{_USER_BLACKLIST_PREFIX}{user_id}'
    try:
        revocation_cache().set(key, '1', ttl)
    except Exception as e:
        logger.error(f"Cannot blacklist user {user_id}: {e}")
        return False
    logger.info(f"User blacklisted: {user_id} for {ttl}s")
    return True


def unblacklist_user(user_id: str) -> DropReport:
    """Lift a user ban; reports what that actually did to the store.

    ``blacklist_user`` above has documented since 0.39.0 that "a caller that
    ignores the result cannot tell a ban from a no-op". That concern was never
    carried across to the delete path: until 0.47.0 this returned ``True`` for
    "the call did not raise", which is the same value whether the ban was
    lifted, was never there, or is still readable afterwards — and lifting a
    ban that is still in force leaves a user refused by every service in the
    fleet while the operator has been told they are back.

    Now it measures — read, delete, read back — and reports a
    :class:`~stapel_core.core.drop.DropReport`, truthy only for ``DROPPED``.
    ``NOT_FOUND`` means nothing was banned under THIS deployment's revocation
    namespace, which is worth checking against the service that issued the ban
    before telling anyone the ban is gone.
    """
    key = f'{_USER_BLACKLIST_PREFIX}{user_id}'
    return drop_cache_key(
        revocation_cache,
        key,
        what="user ban",
        namespace=revocation_namespace(),
        log=logger,
        hint=(
            "check STAPEL_JWT_REVOCATION_NAMESPACE/_CACHE agree with the "
            "service that issued the ban"
        ),
    )


def is_user_blacklisted(user_id: str) -> bool:
    """Check if a user is blacklisted.

    Fails CLOSED, matching ``stapel_core.core.token_blacklist.TokenBlacklist``:
    with the store unreachable, answering "not banned" resurrects every banned
    session exactly when the system is degraded, and a ban is the one answer an
    operator issues because they cannot wait. Override with
    ``STAPEL_BLACKLIST_FAIL_OPEN`` for availability-over-security deployments.
    """
    key = f'{_USER_BLACKLIST_PREFIX}{user_id}'
    try:
        return bool(revocation_cache().get(key))
    except Exception as e:
        logger.error(f"Error checking user blacklist for {user_id}: {e}")
        return not _blacklist_fail_open()


#: HTTP methods that carry no side effect, so no proof is asked for them.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: The custom header ``CsrfExemptAPIMiddleware`` already names as the
#: same-origin proof: a cross-origin page cannot set it without a CORS
#: preflight this deployment never answers with an allow-list echo.
CSRF_PROOF_HEADER = "HTTP_X_REQUESTED_WITH"
CSRF_PROOF_HEADER_VALUE = "XMLHttpRequest"


def _origin_is_ours(request, origin: str) -> bool:
    """Django's own CSRF origin rule, applied to one header value.

    Same scheme+host+port as the request, or an entry of
    ``CSRF_TRUSTED_ORIGINS`` (wildcards included — ``is_same_domain`` is the
    function Django's ``CsrfViewMiddleware`` uses for them).
    """
    from urllib.parse import urlsplit

    from django.conf import settings
    from django.utils.http import is_same_domain

    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if not parts.scheme or not parts.netloc:
        return False

    good_scheme = "https" if request.is_secure() else "http"
    try:
        host = request.get_host()
    except Exception:  # DisallowedHost — the request is refused elsewhere
        return False
    if parts.scheme == good_scheme and parts.netloc == host:
        return True

    for entry in getattr(settings, "CSRF_TRUSTED_ORIGINS", None) or []:
        try:
            trusted = urlsplit(entry)
        except ValueError:
            continue
        if not trusted.scheme or not trusted.netloc:
            continue
        if parts.scheme != trusted.scheme:
            continue
        # "*.example.com" -> ".example.com", the shape is_same_domain reads as
        # "this host or any subdomain of it" — Django's own conversion.
        if is_same_domain(parts.netloc, trusted.netloc.lstrip("*")):
            return True
    return False


#: The switch that turns the guard below into an automatic one.
COOKIE_CSRF_SETTING = "STAPEL_JWT_COOKIE_CSRF"


def cookie_csrf_enforced() -> bool:
    """Does :class:`JWTCookieAuthentication` run the guard by itself?

    ``STAPEL_JWT_COOKIE_CSRF``, **default False**, and the default is the
    honest one rather than the brave one: requiring a same-origin proof is a
    change to what every existing client must send, and a client that sends
    neither ``X-Requested-With`` nor an ``Origin`` — every Django test client
    in the fleet, and any non-browser caller that authenticates with the
    cookie — starts getting 403 the moment the library is upgraded. That is a
    deployment's cutover to schedule, not a library's to impose on a patch
    release. ``SameSite=Lax`` is what stands in the meantime, which is what
    stood before.

    Turning it on is one line, and a deployment whose browser client uses
    ``fetch``/``XMLHttpRequest`` needs nothing else: a same-origin ``fetch``
    POST sends ``Origin``.

    :func:`enforce_cookie_csrf` itself is NOT gated by this — a host that
    calls it from its own authenticator has already decided.
    """
    from django.conf import settings

    return bool(getattr(settings, COOKIE_CSRF_SETTING, False))


def cookie_csrf_proof_ok(request) -> bool:
    """Has this cookie-authenticated request proved it is not cross-site?

    Two proofs, either of which is enough:

    * ``X-Requested-With: XMLHttpRequest`` — a custom header, so a cross-origin
      page needs a CORS preflight to send it, and this deployment's CORS does
      not echo a foreign origin. This is the proof
      :class:`~stapel_core.django.jwt.middleware.CsrfExemptAPIMiddleware`
      already describes; it simply never reached a DRF view, because DRF views
      are ``csrf_exempt`` and ``CsrfViewMiddleware`` skips them.
    * an ``Origin`` (or, absent one, a ``Referer``) this deployment serves —
      the same two headers Django's CSRF machinery reads, so a browser that
      sends neither the custom header nor an origin is the shape a cross-site
      form POST has.

    Safe methods always pass: the guard is about side effects.
    """
    if request.method in _SAFE_METHODS:
        return True
    if request.META.get(CSRF_PROOF_HEADER) == CSRF_PROOF_HEADER_VALUE:
        return True
    origin = request.META.get("HTTP_ORIGIN")
    if origin:
        return _origin_is_ours(request, origin)
    referer = request.META.get("HTTP_REFERER")
    if referer:
        return _origin_is_ours(request, referer)
    return False


def enforce_cookie_csrf(request) -> None:
    """Refuse a cookie-only mutation that cannot prove it is same-origin.

    The guard for every cookie-first DRF authenticator — this package's
    :class:`JWTCookieAuthentication` calls it, and a host that ships its own
    cookie authenticator should call it too rather than re-deriving the rule.

    Raises DRF's :class:`~rest_framework.exceptions.PermissionDenied` (HTTP
    **403**, the same status and the same shape DRF's own
    ``SessionAuthentication`` raises for a failed CSRF check), so the answer is
    distinguishable from "no credential" (401) in a log.

    A request whose credential arrived in the ``Authorization`` header never
    gets here: a bearer is not ambient, an attacker's page cannot produce one,
    and asking a service-to-service client for a browser proof would refuse
    every legitimate one.
    """
    if cookie_csrf_proof_ok(request):
        return
    from rest_framework import exceptions

    logger.warning(
        "JWT cookie CSRF guard - refused a cookie-only %s to %s with no "
        "same-origin proof (origin=%r, referer=%r)",
        request.method,
        request.path,
        request.META.get("HTTP_ORIGIN"),
        request.META.get("HTTP_REFERER"),
    )
    raise exceptions.PermissionDenied(
        "CSRF Failed: a cookie-authenticated request must prove it is "
        "same-origin. Send X-Requested-With: XMLHttpRequest, or an Origin "
        "this deployment serves, or authenticate with the Authorization "
        "header instead."
    )


class JWTCookieAuthentication(authentication.BaseAuthentication):
    """
    DRF authentication class that uses JWT from cookies.

    Uses unified jwt_provider for all JWT operations.

    Usage:
        In settings.py:
        REST_FRAMEWORK = {
            'DEFAULT_AUTHENTICATION_CLASSES': [
                'stapel_core.django.jwt.authentication.JWTCookieAuthentication',
            ],
        }
    """

    def authenticate(self, request):
        """
        Authenticate the request using JWT from cookies.

        Args:
            request: Django request object

        Returns:
            tuple: (user, None) if authenticated, None otherwise
        """
        from .utils import extract_jwt_from_request, get_or_create_user_from_jwt, jwt_cookie_names
        from .provider import jwt_provider

        # Extract JWT from cookies
        access_token, _ = extract_jwt_from_request(request)

        if not access_token:
            return None

        # The credential is ambient exactly when it came from the cookie: the
        # browser attached it without the page asking. Ask that request — and
        # only that request — for the same-origin proof before spending a
        # token validation on it. A bearer in the Authorization header is a
        # credential the caller chose to send and is never gated.
        if cookie_csrf_enforced() and (
            request.COOKIES.get(jwt_cookie_names()[0]) == access_token
        ):
            enforce_cookie_csrf(request)

        # Extract metadata for debugging
        user_agent = request.headers.get('user-agent', 'unknown')
        client_ip = self._get_client_ip(request)
        token_suffix = access_token[-10:] if len(access_token) >= 10 else 'short_token'
        path = request.path

        try:
            # Check if token is blacklisted
            if jwt_provider.is_blacklisted(access_token):
                logger.warning(
                    f"JWT Auth Failed - Blacklisted token - "
                    f"token_suffix={token_suffix}, "
                    f"client_ip={client_ip}, "
                    f"user_agent={user_agent}, "
                    f"path={path}"
                )
                return None

            # Validate and get user data from token
            user_data = jwt_provider.validate_token(access_token)

            if not user_data:
                logger.warning(
                    f"JWT Auth Failed - Invalid token - "
                    f"token_suffix={token_suffix}, "
                    f"client_ip={client_ip}, "
                    f"user_agent={user_agent}, "
                    f"path={path}"
                )
                return None

            # Check if user is banned (user-level blacklist)
            user_id = user_data.get('user_id')
            if user_id and is_user_blacklisted(user_id):
                logger.warning(
                    f"JWT Auth Failed - User blacklisted - "
                    f"user_id={user_id}, "
                    f"token_suffix={token_suffix}, "
                    f"path={path}"
                )
                return None

            # Get or create user from JWT data
            user = get_or_create_user_from_jwt(user_data)

            if not user:
                logger.error(
                    f"JWT Auth Failed - User creation failed - "
                    f"user_id={user_data.get('user_id', 'unknown')}, "
                    f"token_suffix={token_suffix}, "
                    f"client_ip={client_ip}, "
                    f"path={path}"
                )
                return None

            return (user, None)

        except Exception as e:
            logger.error(
                f"JWT Auth Failed - Exception - "
                f"error_type={type(e).__name__}, "
                f"error_msg={str(e)}, "
                f"token_suffix={token_suffix}, "
                f"client_ip={client_ip}, "
                f"user_agent={user_agent}, "
                f"path={path}",
                exc_info=True
            )
            return None

    def _get_client_ip(self, request):
        """Extract client IP from request, handling proxies"""
        x_forwarded_for = request.headers.get('x-forwarded-for')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', 'unknown')

    def authenticate_header(self, request):
        """
        Return the WWW-Authenticate header value.

        This is shown in 401 responses to indicate the authentication scheme.
        Uses ASCII-only characters to comply with ISO-8859-1 encoding requirement.
        """
        return 'Bearer'
