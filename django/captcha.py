"""Django/DRF captcha integration.

Usage in a serializer::

    from stapel_core.django.captcha import CaptchaMixin

    class MySerializer(CaptchaMixin, serializers.Serializer):
        email = serializers.EmailField()
        captcha_token = serializers.CharField(required=False, allow_blank=True)

        def validate(self, attrs):
            self._require_captcha_if_configured(attrs)
            return attrs

Per-service Django settings::

    CAPTCHA_BACKEND = 'turnstile'   # or 'recaptcha' | 'hcaptcha' | 'noop' | dotted.path
    CAPTCHA_SECRET  = env.str('CAPTCHA_SECRET', None)  # absent → captcha disabled
"""

import logging

from stapel_core.captcha import NoopVerifier, build_verifier

logger = logging.getLogger(__name__)


def _extract_ip(request) -> str | None:
    """Extract the real client IP from a Django request."""
    if not request:
        return None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    for candidate in forwarded.split(','):
        candidate = candidate.strip()
        if candidate:
            return candidate
    return request.META.get('HTTP_X_REAL_IP') or request.META.get('REMOTE_ADDR') or None


def get_verifier() -> 'CaptchaVerifier':  # noqa: F821
    """Build a verifier from Django settings.

    Returns ``NoopVerifier`` when ``CAPTCHA_SECRET`` is absent or empty,
    making captcha effectively disabled with no extra toggle needed.
    """
    from django.conf import settings
    backend = getattr(settings, 'CAPTCHA_BACKEND', 'noop')
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
