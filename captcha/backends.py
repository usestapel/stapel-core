"""Captcha verifier backends.

Usage:
    from stapel_core.captcha import build_verifier

    verifier = build_verifier('turnstile', secret='your-secret')
    is_valid = verifier.verify(token, ip='1.2.3.4')

Custom backend (dotted import path in CAPTCHA_BACKEND setting):
    class MyCaptchaVerifier(CaptchaVerifier):
        def verify(self, token, ip=None):
            return my_service.check(token, self.secret)
"""

import logging
from abc import ABC, abstractmethod

import requests

logger = logging.getLogger(__name__)


class CaptchaVerifier(ABC):
    """Base class for all captcha backends.

    Subclass and implement ``verify()`` to add a custom backend, then set
    ``CAPTCHA_BACKEND = 'myapp.captcha.MyVerifier'`` in Django settings.
    """

    def __init__(self, secret: str | None = None):
        self.secret = secret

    @abstractmethod
    def verify(self, token: str, ip: str | None = None) -> bool:
        """Return True if the captcha token is valid, False otherwise.

        Never raise — network errors or service outages should return False
        so the endpoint stays available while logging the incident.
        """


class TurnstileVerifier(CaptchaVerifier):
    """Cloudflare Turnstile backend.

    Requires a Turnstile secret key (server-side key from the Cloudflare dashboard).
    """

    _VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'

    def verify(self, token: str, ip: str | None = None) -> bool:
        payload = {'secret': self.secret, 'response': token}
        if ip:
            payload['remoteip'] = ip
        try:
            response = requests.post(self._VERIFY_URL, data=payload, timeout=5)
            result = response.json()
            return bool(result.get('success'))
        except Exception:
            logger.exception('Turnstile verification request failed')
            return False


class RecaptchaVerifier(CaptchaVerifier):
    """Google reCAPTCHA v2 backend.

    Requires a reCAPTCHA secret key from the Google reCAPTCHA admin console.
    """

    _VERIFY_URL = 'https://www.google.com/recaptcha/api/siteverify'

    def verify(self, token: str, ip: str | None = None) -> bool:
        payload = {'secret': self.secret, 'response': token}
        if ip:
            payload['remoteip'] = ip
        try:
            response = requests.post(self._VERIFY_URL, data=payload, timeout=5)
            result = response.json()
            return bool(result.get('success'))
        except Exception:
            logger.exception('reCAPTCHA verification request failed')
            return False


class HcaptchaVerifier(CaptchaVerifier):
    """hCaptcha backend.

    Requires an hCaptcha secret key from hcaptcha.com.
    """

    _VERIFY_URL = 'https://hcaptcha.com/siteverify'

    def verify(self, token: str, ip: str | None = None) -> bool:
        payload = {'secret': self.secret, 'response': token}
        if ip:
            payload['remoteip'] = ip
        try:
            response = requests.post(self._VERIFY_URL, data=payload, timeout=5)
            result = response.json()
            return bool(result.get('success'))
        except Exception:
            logger.exception('hCaptcha verification request failed')
            return False


class NoopVerifier(CaptchaVerifier):
    """Always passes — for tests, development, and the disabled state.

    ``build_verifier`` returns this when no secret is configured.
    """

    def verify(self, token: str, ip: str | None = None) -> bool:
        return True


_BUILTIN_BACKENDS: dict[str, type[CaptchaVerifier]] = {
    'turnstile': TurnstileVerifier,
    'recaptcha': RecaptchaVerifier,
    'hcaptcha': HcaptchaVerifier,
    'noop': NoopVerifier,
}


def build_verifier(backend: str, secret: str | None) -> CaptchaVerifier:
    """Return a configured verifier instance.

    Args:
        backend: Short name (``'turnstile'``, ``'recaptcha'``, ``'hcaptcha'``,
                 ``'noop'``) or a dotted import path to a custom
                 ``CaptchaVerifier`` subclass (e.g. ``'myapp.captcha.MyVerifier'``).
        secret:  Backend secret key.  If ``None`` or empty, returns
                 ``NoopVerifier`` regardless of backend — captcha is effectively
                 disabled.

    Raises:
        ImportError: If a dotted-path backend cannot be imported.
        TypeError: If a dotted-path class does not subclass ``CaptchaVerifier``.
    """
    if not secret:
        return NoopVerifier()

    cls = _BUILTIN_BACKENDS.get(backend)
    if cls is None:
        import importlib
        module_path, class_name = backend.rsplit('.', 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        if not (isinstance(cls, type) and issubclass(cls, CaptchaVerifier)):
            raise TypeError(f'{backend!r} must subclass CaptchaVerifier')

    return cls(secret)
