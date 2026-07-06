"""System checks for the secret-provider seam.

W-level by design: the default env provider always works, so an unconfigured
project never trips these. A *custom* provider that cannot be imported or is
the wrong shape would raise loudly at the first :func:`get_secret` anyway —
the check surfaces the misconfiguration at ``manage.py check`` time without
blocking unrelated management commands, and deliberately does **not** probe
connectivity (a Vault that is unreachable at check time may be reachable at
runtime; that is not a code-level misconfiguration).
"""
from django.core import checks

W001_UNIMPORTABLE = "stapel_core.secrets.W001"
W002_NOT_A_PROVIDER = "stapel_core.secrets.W002"


@checks.register("stapel_secrets")
def check_secrets_provider(app_configs=None, **kwargs):
    from django.utils.module_loading import import_string

    from .conf import secrets_settings

    value = secrets_settings.PROVIDER
    if isinstance(value, str):
        try:
            value = import_string(value)
        except ImportError as exc:
            return [checks.Warning(
                f"STAPEL_SECRETS['PROVIDER'] ({secrets_settings.PROVIDER!r}) "
                f"cannot be imported: {exc}. get_secret() will raise on the "
                "first secret read.",
                hint="Point PROVIDER at a class/instance with a "
                     "get(name) -> str | None method (e.g. "
                     "stapel_vault.VaultSecretProvider), or leave the env "
                     "default.",
                id=W001_UNIMPORTABLE,
            )]
    target = value() if isinstance(value, type) else value
    if not (hasattr(target, "get") and callable(target.get)):
        return [checks.Warning(
            f"STAPEL_SECRETS['PROVIDER'] resolved to {value!r}, which has no "
            "callable get(name) — it is not a SecretProvider.",
            hint="Implement get(self, name) -> str | None on the provider.",
            id=W002_NOT_A_PROVIDER,
        )]
    return []


__all__ = ["check_secrets_provider", "W001_UNIMPORTABLE", "W002_NOT_A_PROVIDER"]
