"""Settings namespace for captcha + challenge policy (``STAPEL_CAPTCHA``)."""
from stapel_core.conf import AppSettings

captcha_settings = AppSettings(
    "STAPEL_CAPTCHA",
    defaults={
        # None → 'noop'. NOTE: get_verifier reads BACKEND/SECRET from the
        # STAPEL_CAPTCHA dict directly (no env fallback) so a stray generic
        # `SECRET` env var can never silently enable captcha; they are
        # listed here for discoverability.
        "BACKEND": None,
        # Unset → NoopVerifier → captcha disabled.
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
