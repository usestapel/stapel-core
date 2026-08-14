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
    # Every key here decides whether, and how hard, a user is challenged
    # before a privileged action — and every name is generic enough to
    # collide with something else in a container's environment. Without this
    # list, `DEFAULT_LEVEL=opt_in` in the environment silently turned step-up
    # off for every @requires_verification(level=None) view, and any
    # `DEFAULT_FACTORS` env var arrived as a *str* (so list("...") became
    # single characters, available_for() found nothing, and default_on views
    # passed straight through to the handler). Values still resolve from the
    # STAPEL_VERIFICATION dict, a flat Django setting, or the default — an
    # env var is simply ignored. Same reason as access/, netintel/, gateway/,
    # secrets/, security/ and media/; this namespace was the one that missed it.
    no_env=(
        "DEFAULT_FACTORS",
        "DEFAULT_MAX_AGE",
        "CHALLENGE_TTL",
        "MAX_ATTEMPTS",
        "EXTRA_FACTORS",
        "DEFAULT_LEVEL",
        "POLICY_CACHE_TTL",
    ),
)

__all__ = ["verification_settings"]
