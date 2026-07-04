"""Settings namespace for captcha + challenge policy (``STAPEL_CAPTCHA``).

Legacy compatibility: captcha historically configured via the flat Django
settings ``CAPTCHA_BACKEND`` / ``CAPTCHA_SECRET``. Those keep working —
``BACKEND`` / ``SECRET`` here default to ``None``, and
``stapel_core.django.captcha.get_verifier`` falls back to the flat settings
when the namespaced keys are unset. New keys (challenge matrix/policy) exist
only in the namespace.
"""
from stapel_core.conf import AppSettings

captcha_settings = AppSettings(
    "STAPEL_CAPTCHA",
    defaults={
        # None → fall back to the legacy flat CAPTCHA_BACKEND ('noop').
        # NOTE: get_verifier reads BACKEND/SECRET from the STAPEL_CAPTCHA
        # dict directly (no env fallback) so a stray generic `SECRET` env
        # var can never silently enable captcha; they are listed here for
        # discoverability.
        "BACKEND": None,
        # None → fall back to the legacy flat CAPTCHA_SECRET (unset →
        # NoopVerifier → captcha disabled).
        "SECRET": None,
        # ip-kind → challenge level overrides, MERGED over the builtin
        # DEFAULT_CHALLENGE_MATRIX (captcha/policy.py) — a partial dict
        # only overrides the listed kinds.
        "CHALLENGE_MATRIX": {},
        # Per-action overrides: {action: {kind: level} | "+1"}.
        # "+1" bumps the matrix level one step (saturating at "block");
        # a per-kind value may itself be "+1".
        "ACTION_OVERRIDES": {},
        # Replace-style dotted-path seam for the whole policy.
        "CHALLENGE_POLICY": "stapel_core.captcha.policy.MatrixChallengePolicy",
    },
)

__all__ = ["captcha_settings"]
