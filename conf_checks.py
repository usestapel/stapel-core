"""System check: an environment variable that is set and silently ignored.

A key in ``import_strings`` names the class the process imports and runs, so
``AppSettings`` stopped consulting the environment for it. That is the safe
behaviour, and it is a SILENT one: a deployment that used to select an
implementation with a bare env var now runs the settings value or the default,
with no error and no log line — the operator's belief about which code is on
the privileged path is simply wrong, and nothing says so.

The upgrade note could only offer "grep your manifests". This is the same
obligation as a mechanism: ``manage.py check`` names the variable.

Warning, not Error. The process is running the safe implementation; what is
broken is the operator's intent, and blocking a deploy over a stray env var
would punish the safe state.
"""
from __future__ import annotations

from django.core import checks

W001_ENV_VAR_IGNORED = "stapel_core.conf.W001"


@checks.register("stapel_conf")
def check_ignored_env_vars(app_configs=None, **kwargs):
    from .conf import registered_settings

    warnings = []
    seen: set[tuple[str, str]] = set()
    for app_settings in registered_settings():
        for key, env_name in app_settings.ignored_env_vars():
            # A namespace can be constructed more than once in a process
            # (reloads, test doubles); the operator needs the finding once.
            if (app_settings.namespace, env_name) in seen:
                continue
            seen.add((app_settings.namespace, env_name))
            warnings.append(checks.Warning(
                f"Environment variable {env_name} is set but ignored: "
                f"{app_settings.namespace}[{key!r}] is an import_strings key, "
                "and such a key names the class the process loads, so it is "
                "never read from the environment. This service is running the "
                "settings value or the default instead of what that variable "
                "says.",
                hint=(
                    f"Move the value into the {app_settings.namespace} dict in "
                    f"your settings module (recommended), or — if this "
                    f"deployment really must select the implementation from the "
                    f"environment — add {key!r} to env_overridable= in that "
                    f"namespace's AppSettings declaration (and remove it from "
                    f"no_env= if listed there). Unsetting the variable also "
                    "clears this warning."
                ),
                id=W001_ENV_VAR_IGNORED,
            ))
    return warnings


__all__ = ["check_ignored_env_vars", "W001_ENV_VAR_IGNORED"]
