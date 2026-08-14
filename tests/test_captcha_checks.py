"""A named captcha backend that cannot challenge is a boot failure.

`BACKEND='turnstile'` with a rotated-away, typo'd or unmounted secret used to
build a NoopVerifier, which passes every token, and every consumer reads that
as "captcha is off". The brute-force floor under OTP request, password reset
and magic link disappeared with nothing to see. The operator should meet that
at `manage.py check`, not as a 500 on somebody's password reset — and never as
silence.
"""
from django.test import override_settings

from stapel_core.django.captcha_checks import (
    E001_BACKEND_WITHOUT_SECRET,
    E002_BACKEND_UNUSABLE,
    check_captcha_backend,
)


def _ids(errors):
    return [e.id for e in errors]


@override_settings(STAPEL_CAPTCHA={})
def test_unconfigured_captcha_is_silent():
    """No backend named is how a deployment says "no captcha"."""
    assert check_captcha_backend() == []


@override_settings(STAPEL_CAPTCHA={"BACKEND": "noop"})
def test_explicit_noop_is_silent():
    assert check_captcha_backend() == []


@override_settings(STAPEL_CAPTCHA={"BACKEND": "turnstile", "SECRET": "s"})
def test_configured_captcha_is_silent():
    assert check_captcha_backend() == []


@override_settings(STAPEL_CAPTCHA={"BACKEND": "turnstile"})
def test_backend_without_secret_is_an_error():
    errors = check_captcha_backend()
    assert _ids(errors) == [E001_BACKEND_WITHOUT_SECRET]
    assert errors[0].level >= 40


@override_settings(STAPEL_CAPTCHA={"BACKEND": "turnstile", "SECRET": ""})
def test_empty_secret_is_an_error():
    assert _ids(check_captcha_backend()) == [E001_BACKEND_WITHOUT_SECRET]


@override_settings(
    STAPEL_CAPTCHA={"BACKEND": "stapel_core.captcha.backends.logger", "SECRET": "s"}
)
def test_unusable_backend_is_an_error():
    """A dotted path that resolves to something that is not a verifier."""
    assert _ids(check_captcha_backend()) == [E002_BACKEND_UNUSABLE]
