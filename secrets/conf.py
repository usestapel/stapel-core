"""Settings namespace for secret resolution (``STAPEL_SECRETS``)."""
from stapel_core.conf import AppSettings

secrets_settings = AppSettings(
    "STAPEL_SECRETS",
    defaults={
        # Provider seam (replace-style): dotted path, class or instance with a
        # ``get(name) -> str | None`` method. Default reads os.environ — local
        # dev / the minimal preset keep working with zero dependencies. Point
        # it at stapel_vault.VaultSecretProvider to move production secrets off
        # the environment. NOTE: production settings modules resolve SECRET_KEY
        # *before* django.setup(); use the STAPEL_SECRETS_PROVIDER env var to
        # select the provider at that bootstrap stage (this key is read after
        # Django is configured).
        "PROVIDER": "stapel_core.secrets.EnvSecretProvider",
        # Per-process value cache TTL in seconds. Also the rotation re-read
        # window: after it elapses get_secret re-reads the provider so a
        # rotated secret propagates without a restart. 0 disables caching
        # (every get_secret hits the provider — only for a store that is both
        # cheap and where read-your-writes matters more than latency).
        "CACHE_TTL": 300,
    },
    # PROVIDER decides which code reads every secret in the process — a
    # generic name that must never be silently sourced from a stray same-named
    # env var. The intentional bootstrap override is the explicit
    # STAPEL_SECRETS_PROVIDER var (read in stapel_core.secrets), not this key.
    no_env=("PROVIDER",),
)

__all__ = ["secrets_settings"]
