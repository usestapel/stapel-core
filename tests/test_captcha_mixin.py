"""Tests for stapel_core.django.captcha — _extract_ip + CaptchaMixin gating."""

import pytest
from django.test import override_settings
from rest_framework import serializers

from stapel_core.django.captcha import CaptchaMixin, _extract_ip, get_verifier
from stapel_core.captcha import NoopVerifier
from stapel_core.django.api.errors import StapelValidationError


class _Req:
    """Minimal request stub carrying a META mapping."""


class TestExtractIp:
    def test_x_forwarded_for_first(self):
        req = _Req()
        req.META = {"HTTP_X_FORWARDED_FOR": "  203.0.113.5 , 10.0.0.1"}
        assert _extract_ip(req) == "203.0.113.5"

    def test_falls_back_to_x_real_ip(self):
        req = _Req()
        req.META = {"HTTP_X_REAL_IP": "198.51.100.7"}
        assert _extract_ip(req) == "198.51.100.7"

    def test_falls_back_to_remote_addr(self):
        req = _Req()
        req.META = {"REMOTE_ADDR": "192.0.2.9"}
        assert _extract_ip(req) == "192.0.2.9"

    def test_none_request(self):
        assert _extract_ip(None) is None


class TestGetVerifier:
    def test_noop_when_secret_absent(self):
        with override_settings(CAPTCHA_BACKEND="turnstile"):
            assert isinstance(get_verifier(), NoopVerifier)

    def test_real_backend_when_secret_set(self):
        from stapel_core.captcha import TurnstileVerifier
        with override_settings(CAPTCHA_BACKEND="turnstile", CAPTCHA_SECRET="s"):
            v = get_verifier()
        assert isinstance(v, TurnstileVerifier)


class _CaptchaSerializer(CaptchaMixin, serializers.Serializer):
    captcha_token = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        self._require_captcha_if_configured(attrs)
        return attrs


class TestCaptchaMixin:
    def test_required_raises_when_active_and_token_missing(self):
        with override_settings(CAPTCHA_BACKEND="turnstile", CAPTCHA_SECRET="s"):
            ser = _CaptchaSerializer(data={}, context={"request": None})
            with pytest.raises(StapelValidationError):
                ser._require_captcha_if_configured({})

    def test_skipped_when_disabled(self):
        # No CAPTCHA_SECRET -> NoopVerifier -> _require is a no-op.
        with override_settings(CAPTCHA_BACKEND="turnstile"):
            ser = _CaptchaSerializer(data={}, context={"request": None})
            assert ser._require_captcha_if_configured({}) is None
