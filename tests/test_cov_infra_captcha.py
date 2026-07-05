"""Coverage tests for the remaining captcha/backends.py and django/captcha.py lines."""
from unittest import mock

import pytest
from django.test import override_settings

from stapel_core.captcha import HcaptchaVerifier, RecaptchaVerifier
from stapel_core.django.captcha import CaptchaMixin
from stapel_core.django.errors import StapelValidationError


# ---------------------------------------------------------------------------
# captcha/backends.py — ip forwarding + hcaptcha error path
# ---------------------------------------------------------------------------


def test_recaptcha_forwards_ip():
    with mock.patch("requests.post") as post:
        post.return_value.json.return_value = {"success": True}
        assert RecaptchaVerifier("secret").verify("tok", ip="5.5.5.5") is True
    assert post.call_args[1]["data"]["remoteip"] == "5.5.5.5"


def test_hcaptcha_forwards_ip():
    with mock.patch("requests.post") as post:
        post.return_value.json.return_value = {"success": True}
        assert HcaptchaVerifier("secret").verify("tok", ip="6.6.6.6") is True
    assert post.call_args[1]["data"]["remoteip"] == "6.6.6.6"


def test_hcaptcha_network_error_returns_false():
    with mock.patch("requests.post", side_effect=ConnectionError("down")):
        assert HcaptchaVerifier("secret").verify("tok") is False


# ---------------------------------------------------------------------------
# django/captcha.py — CaptchaMixin.validate_captcha_token (active verifier)
# ---------------------------------------------------------------------------


class _Request:
    def __init__(self, meta=None):
        self.META = meta or {}


class _Holder(CaptchaMixin):
    def __init__(self, request=None):
        self.context = {"request": request}


def test_validate_captcha_token_noop_passthrough():
    # no CAPTCHA_SECRET -> NoopVerifier -> token accepted untouched
    with override_settings(CAPTCHA_BACKEND="turnstile"):
        assert _Holder().validate_captcha_token("anything") == "anything"


def test_validate_captcha_token_success_forwards_ip():
    # remoteip uses netintel.client_ip's trust model (REMOTE_ADDR by default),
    # consistent with classification — not a spoofable X-Forwarded-For header.
    request = _Request({"REMOTE_ADDR": "203.0.113.5"})
    with override_settings(CAPTCHA_BACKEND="turnstile", CAPTCHA_SECRET="s"):
        with mock.patch("requests.post") as post:
            post.return_value.json.return_value = {"success": True}
            assert _Holder(request).validate_captcha_token("tok") == "tok"
    assert post.call_args[1]["data"]["remoteip"] == "203.0.113.5"


def test_validate_captcha_token_failure_raises():
    with override_settings(CAPTCHA_BACKEND="turnstile", CAPTCHA_SECRET="s"):
        with mock.patch("requests.post") as post:
            post.return_value.json.return_value = {"success": False}
            with pytest.raises(StapelValidationError):
                _Holder(_Request()).validate_captcha_token("bad-tok")


def test_validate_captcha_token_network_error_raises():
    # verifier returns False on network errors -> token rejected (fail closed)
    with override_settings(CAPTCHA_BACKEND="turnstile", CAPTCHA_SECRET="s"):
        with mock.patch("requests.post", side_effect=ConnectionError("down")):
            with pytest.raises(StapelValidationError):
                _Holder(None).validate_captcha_token("tok")
