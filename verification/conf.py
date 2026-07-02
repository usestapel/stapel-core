"""Settings namespace for step-up verification."""
from stapel_core.conf import AppSettings

verification_settings = AppSettings(
    "STAPEL_VERIFICATION",
    defaults={
        # Factors offered when a view doesn't specify its own list.
        "DEFAULT_FACTORS": ["otp_email", "totp", "passkey"],
        # Grant lifetime (seconds) when a view doesn't pass max_age.
        "DEFAULT_MAX_AGE": 300,
        # Challenge lifetime (seconds): how long the client has to complete
        # a factor after receiving the 403 envelope.
        "CHALLENGE_TTL": 600,
        # Failed factor attempts before the challenge is invalidated.
        "MAX_ATTEMPTS": 5,
        # Extra factor classes to register at startup (dotted paths).
        "EXTRA_FACTORS": [],
        # Policy level applied when a view passes level=None:
        # "strict" | "default_on" | "opt_in".
        "DEFAULT_LEVEL": "strict",
        # How long (seconds) a user's resolved verification policy
        # (auth.verification.policy Function result) stays cached.
        "POLICY_CACHE_TTL": 60,
    },
)

__all__ = ["verification_settings"]
