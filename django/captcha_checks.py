"""System checks for captcha configuration (tag ``stapel_captcha``).

Captcha is the brute-force floor under OTP request, password reset and magic
link. It used to disable itself in silence: ``build_verifier`` answered a
missing secret with ``NoopVerifier``, which passes every token, and every
consumer reads ``isinstance(verifier, NoopVerifier)`` as "captcha is off".
So ``BACKEND='turnstile'`` with a rotated-away, typo'd or unmounted secret
looked exactly like a healthy deployment, and the protection was gone with
nothing to notice.

``build_verifier`` now raises for that shape. This check is where an operator
should meet it — at ``manage.py check`` / boot smoke, with the setting named —
rather than as a 500 on somebody's password reset.

E-level throughout: every finding here means a *stated* intent to challenge
that the process cannot carry out. A deployment that wants no captcha states
that by leaving ``BACKEND`` unset (or setting it to ``noop``), and this check
says nothing at all.
"""
from __future__ import annotations

from django.core import checks

E001_BACKEND_WITHOUT_SECRET = "stapel_core.captcha.E001"
E002_BACKEND_UNUSABLE = "stapel_core.captcha.E002"


@checks.register("stapel_captcha")
def check_captcha_backend(app_configs=None, **kwargs):
    from django.conf import settings

    from stapel_core.captcha import CaptchaConfigurationError

    # Read the namespace dict directly, exactly as get_verifier() does, so the
    # check and the runtime agree about what is configured.
    overrides = getattr(settings, "STAPEL_CAPTCHA", None) or {}
    backend = overrides.get("BACKEND") or "noop"
    if backend == "noop":
        return []

    from stapel_core.django.captcha import get_verifier

    try:
        get_verifier()
    except CaptchaConfigurationError:
        return [checks.Error(
            f"STAPEL_CAPTCHA names the {backend!r} backend but no SECRET, so "
            "no token can be verified. Captcha is the brute-force floor under "
            "OTP request, password reset and magic link.",
            hint='Set STAPEL_CAPTCHA["SECRET"] (check that the secret is '
                 "actually mounted in this environment), or set BACKEND to "
                 '"noop" to turn captcha off deliberately.',
            id=E001_BACKEND_WITHOUT_SECRET,
        )]
    except Exception as exc:
        return [checks.Error(
            f"STAPEL_CAPTCHA names the {backend!r} backend, but building it "
            f"raised {type(exc).__name__}: {exc}",
            hint="Fix the dotted path / class (it must subclass "
                 "CaptchaVerifier), or set BACKEND to \"noop\" to turn "
                 "captcha off deliberately.",
            id=E002_BACKEND_UNUSABLE,
        )]
    return []


__all__ = [
    "E001_BACKEND_WITHOUT_SECRET",
    "E002_BACKEND_UNUSABLE",
    "check_captcha_backend",
]
