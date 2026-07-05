"""Django/DRF captcha integration.

Usage in a serializer::

    from stapel_core.django.captcha import CaptchaMixin

    class MySerializer(CaptchaMixin, serializers.Serializer):
        email = serializers.EmailField()
        captcha_token = serializers.CharField(required=False, allow_blank=True)

        def validate(self, attrs):
            self._require_captcha_if_configured(attrs)
            return attrs

Usage on a view method (tiered by network class — see
``stapel_core.captcha.policy``)::

    from stapel_core.django.captcha import captcha_protected

    class RegisterView(APIView):
        @captcha_protected(action="register")
        def post(self, request): ...

Per-service Django settings::

    CAPTCHA_BACKEND = 'turnstile'   # or 'recaptcha' | 'hcaptcha' | 'noop' | dotted.path
    CAPTCHA_SECRET  = env.str('CAPTCHA_SECRET', None)  # absent → captcha disabled

The flat settings above are the legacy spelling and keep working; the
namespaced equivalents are ``STAPEL_CAPTCHA = {"BACKEND": ..., "SECRET":
...}`` plus the challenge-policy keys (``CHALLENGE_MATRIX``,
``ACTION_OVERRIDES``, ``CHALLENGE_POLICY`` — see ``captcha/conf.py``).
"""

import functools
import inspect
import logging

from stapel_core.captcha import NoopVerifier, build_verifier

logger = logging.getLogger(__name__)

ERR_400_CAPTCHA_INVALID = 'error.400.captcha_invalid'
ERR_400_CAPTCHA_REQUIRED = 'error.400.captcha_required'
ERR_403_NETWORK_BLOCKED = 'error.403.network_blocked'


def _register_errors() -> None:
    from stapel_core.django.api.errors import register_service_errors

    register_service_errors({
        ERR_400_CAPTCHA_INVALID: 'Captcha verification failed',
        ERR_400_CAPTCHA_REQUIRED: 'Captcha token is required',
        ERR_403_NETWORK_BLOCKED: 'Requests from this network are not allowed',
    })


_register_errors()


def _extract_ip(request) -> str | None:
    """The client IP as netintel sees it — one trust model per request.

    Delegates to :func:`stapel_core.netintel.client_ip` so the ``remoteip``
    sent to the captcha provider's siteverify and the IP in logs match the IP
    that was *classified* (network-trust tiering). Previously this trusted
    ``X-Forwarded-For`` / ``X-Real-IP`` unconditionally — a different, weaker
    trust model than classification (which trusts ``REMOTE_ADDR`` only unless
    ``STAPEL_NETINTEL["TRUSTED_PROXY_HEADER"]`` is set), so siteverify and the
    logs could disagree with the tiering decision.
    """
    from stapel_core.netintel import client_ip

    return client_ip(request)


def get_verifier() -> 'CaptchaVerifier':  # noqa: F821
    """Build a verifier from Django settings.

    Resolution: ``STAPEL_CAPTCHA["BACKEND"/"SECRET"]`` first, the legacy
    flat ``CAPTCHA_BACKEND`` / ``CAPTCHA_SECRET`` as fallback. Returns
    ``NoopVerifier`` when no secret is configured, making captcha
    effectively disabled with no extra toggle needed.
    """
    from django.conf import settings

    # Read the namespace dict directly (not through AppSettings) so a stray
    # generic `BACKEND`/`SECRET` environment variable can never silently
    # enable or reroute captcha via the AppSettings env fallback.
    overrides = getattr(settings, 'STAPEL_CAPTCHA', None) or {}
    backend = overrides.get('BACKEND')
    if backend is None:
        backend = getattr(settings, 'CAPTCHA_BACKEND', 'noop')
    secret = overrides.get('SECRET')
    if secret is None:
        secret = getattr(settings, 'CAPTCHA_SECRET', None)
    return build_verifier(backend, secret)


class CaptchaMixin:
    """DRF serializer mixin that validates a ``captcha_token`` field.

    Add this mixin **before** ``serializers.Serializer`` in the MRO so that
    ``validate_captcha_token`` runs during DRF field-level validation.

    Call ``self._require_captcha_if_configured(attrs)`` from ``validate()``
    to return ``ERR_400_CAPTCHA_REQUIRED`` when captcha is active but the
    client omitted the token entirely.
    """

    def validate_captcha_token(self, value: str) -> str:
        verifier = get_verifier()
        if isinstance(verifier, NoopVerifier):
            return value
        request = self.context.get('request')
        ip = _extract_ip(request)
        if not verifier.verify(value, ip):
            logger.warning('Captcha verification failed ip=%s', ip)
            from stapel_core.django.errors import StapelValidationError
            raise StapelValidationError('error.400.captcha_invalid')
        return value

    def _require_captcha_if_configured(self, attrs: dict) -> None:
        """Raise if captcha is active but captcha_token was not supplied."""
        verifier = get_verifier()
        if isinstance(verifier, NoopVerifier):
            return
        if not attrs.get('captcha_token'):
            from stapel_core.django.errors import StapelValidationError
            raise StapelValidationError('error.400.captcha_required')


# ---------------------------------------------------------------------------
# @captcha_protected — challenge-policy-driven view protection
# ---------------------------------------------------------------------------


def _find_request(args):
    """The request among a wrapped view's positional args (FBV or method)."""
    for candidate in args[:2]:
        if hasattr(candidate, 'META'):
            return candidate
    return None


def _extract_token(request) -> str | None:
    """Captcha token from the X-Captcha-Token header or a captcha_token field."""
    if request is None:
        return None
    headers = getattr(request, 'headers', None)
    if headers is not None:
        token = headers.get('X-Captcha-Token')
        if token:
            return token
    for source_name in ('data', 'POST'):
        source = getattr(request, source_name, None)
        if source is None:
            continue
        try:
            token = source.get('captcha_token')
        except (TypeError, AttributeError):
            continue
        if token:
            return token
    return None


def _call_verifier(verifier, token: str, ip: str | None, level: str) -> bool:
    """Call ``verifier.verify``, passing ``level`` only if it accepts it.

    Backends may opt into the challenge level via an optional keyword
    (``def verify(self, token, ip=None, *, level=None)``) — e.g. to force
    an interactive challenge; legacy two-argument backends keep working.
    """
    try:
        parameters = inspect.signature(verifier.verify).parameters
    except (TypeError, ValueError):  # builtins/C callables — be conservative
        parameters = {}
    accepts_level = 'level' in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )
    if accepts_level:
        return verifier.verify(token, ip=ip, level=level)
    return verifier.verify(token, ip=ip)


def captcha_protected(action: str = 'default'):
    """Protect a view with the tiered challenge policy (see captcha/policy.py).

    The view declares its endpoint class (``action``), not a hardcoded
    strictness — the policy maps the client's network kind
    (``stapel_core.netintel``) and the action onto a challenge level:

    - ``none`` — request passes, no captcha.
    - ``invisible`` — verify the token if a captcha backend is configured;
      when captcha is unconfigured (NoopVerifier) the request passes,
      exactly like the pre-policy behavior.
    - ``interactive`` / ``interactive+ratelimit`` — verify the token; the
      level is passed to backends that accept it so they can force an
      interactive challenge. Rate limiting is NOT performed here:
      ``request.stapel_challenge_level`` carries the level for rate-limit
      middleware/hosts to consume.
    - ``block`` — reject with 403 ``error.403.network_blocked``. Never
      produced by the default matrix; blocking is an explicit host decision.

    Works on DRF view methods and plain function views::

        class RegisterView(APIView):
            @captcha_protected(action="register")
            def post(self, request): ...

    Every decision is logged at INFO with ``{ip_kind, action, level,
    allowed}`` — the input of host-side antifraud scoring.

    Backward compatibility: with no ``STAPEL_NETINTEL`` provider configured
    the kind is ``unknown`` → level ``invisible`` → identical to the
    historical binary behavior (pass when unconfigured, verify the token
    when a backend is configured).
    """

    def decorator(view_method):
        @functools.wraps(view_method)
        def wrapper(*args, **kwargs):
            from stapel_core.captcha.policy import (
                LEVEL_BLOCK,
                LEVEL_INVISIBLE,
                LEVEL_NONE,
                get_challenge_policy,
            )
            from stapel_core.django.api.errors import StapelErrorResponse
            from stapel_core.netintel import classify_ip, client_ip

            request = _find_request(args)
            ip_kind = classify_ip(client_ip(request)).kind  # cached — cheap

            policy = get_challenge_policy()
            try:
                level = policy.level_for(request, action)
            except Exception:
                logger.exception(
                    'challenge policy failed for action=%s — falling back to '
                    'level=%s', action, LEVEL_INVISIBLE,
                )
                level = LEVEL_INVISIBLE
            if request is not None:
                # Rate-limit hook: middleware/hosts read this to throttle
                # "interactive+ratelimit" clients; captcha does not throttle.
                request.stapel_challenge_level = level

            def _log(allowed: bool) -> None:
                logger.info(
                    'captcha decision ip_kind=%s action=%s level=%s allowed=%s',
                    ip_kind, action, level, allowed,
                )

            if level == LEVEL_NONE:
                _log(True)
                return view_method(*args, **kwargs)
            if level == LEVEL_BLOCK:
                _log(False)
                return StapelErrorResponse(403, ERR_403_NETWORK_BLOCKED)

            verifier = get_verifier()
            if isinstance(verifier, NoopVerifier):
                # Captcha unconfigured — no backend can challenge at any
                # level; identical to the historical disabled state.
                _log(True)
                return view_method(*args, **kwargs)

            token = _extract_token(request)
            ip = _extract_ip(request)
            if not token:
                _log(False)
                return StapelErrorResponse(400, ERR_400_CAPTCHA_REQUIRED)
            if not _call_verifier(verifier, token, ip, level):
                logger.warning('Captcha verification failed ip=%s', ip)
                _log(False)
                return StapelErrorResponse(400, ERR_400_CAPTCHA_INVALID)
            _log(True)
            return view_method(*args, **kwargs)

        return wrapper

    return decorator
