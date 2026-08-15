"""System checks over the AppSettings namespaces: env vars that lie.

W001 — an environment variable that is set and silently ignored.
E002 — an environment variable supplying a container-shaped setting.


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
E002_STRUCTURED_ENV_VAR = "stapel_core.conf.E002"


#: Why the key is closed, in the two vocabularies the rule actually has.
#: The warning must not over-claim: a policy key does not name a class, and
#: telling an operator it does sends them looking for a dotted path that was
#: never there.
_CAUSE = {
    "class": (
        "names the class the process loads, so it is never read from the "
        "environment"
    ),
    "policy": (
        "is declared no_env — its name is generic enough that a stray "
        "same-named variable could silently change a trust or security "
        "decision, so the environment step is closed for it"
    ),
}


def _hint(namespace: str, key: str, family: str) -> str:
    reopen = (
        f"add {key!r} to env_overridable= in that namespace's AppSettings "
        f"declaration (and remove it from no_env= if listed there)"
        if family == "class"
        else f"remove {key!r} from no_env= in that namespace's AppSettings "
             f"declaration"
    )
    return (
        f"Move the value into the {namespace} dict in your settings module "
        f"(recommended), or — if this deployment really must take this key "
        f"from the environment — {reopen}. Unsetting the variable also "
        f"clears this warning."
    )


@checks.register("stapel_conf")
def check_ignored_env_vars(app_configs=None, **kwargs):
    from .conf import registered_settings

    warnings = []
    seen: set[tuple[str, str]] = set()
    for app_settings in registered_settings():
        for key, env_name, family in app_settings.ignored_env_vars():
            # A namespace can be constructed more than once in a process
            # (reloads, test doubles); the operator needs the finding once.
            if (app_settings.namespace, env_name) in seen:
                continue
            seen.add((app_settings.namespace, env_name))
            warnings.append(checks.Warning(
                f"Environment variable {env_name} is set but ignored: "
                f"{app_settings.namespace}[{key!r}] {_CAUSE[family]}. This "
                "service is running the settings value or the default instead "
                "of what that variable says.",
                hint=_hint(app_settings.namespace, key, family),
                id=W001_ENV_VAR_IGNORED,
            ))
    return warnings


@checks.register("stapel_conf")
def check_structured_env_vars(app_configs=None, **kwargs):
    """E002 — a container-shaped setting is being supplied as a bare string.

    ``AppSettings._raw`` refuses such a value the moment the key is read, but
    "the moment the key is read" can be days into a deployment, or never for a
    code path nobody exercises. The whole incident class this repo is closing
    is a misconfiguration that stayed silent because nothing asked at boot, so
    the walk happens at ``manage.py check`` time too.

    Error, not warning: unlike W001 the process is NOT running a safe value.
    It is running whatever ``"auth,profiles"`` iterates to — twelve owners
    named after single characters — and every downstream type check passes.
    """
    from .conf import registered_settings

    errors = []
    seen: set[tuple[str, str]] = set()
    for app_settings in registered_settings():
        for key, env_name in app_settings.structured_env_vars():
            if (app_settings.namespace, env_name) in seen:
                continue
            seen.add((app_settings.namespace, env_name))
            shape = type(app_settings.defaults[key]).__name__
            errors.append(checks.Error(
                f"Environment variable {env_name} is set, but "
                f"{app_settings.namespace}[{key!r}] is declared as a {shape}. "
                "A bare environment string is not one: iterating it yields its "
                "CHARACTERS, and since every character is a str the value "
                "passes downstream type checks as a list of nonsense.",
                hint=(
                    f"Move the value into the {app_settings.namespace} dict in "
                    f"your settings module, or unset {env_name}. This key is "
                    "deliberately not parsed from the environment: any format "
                    "chosen here would be wrong for the container values these "
                    "keys legally hold (a list whose entries may be dicts)."
                ),
                id=E002_STRUCTURED_ENV_VAR,
            ))
    return errors


__all__ = [
    "check_ignored_env_vars",
    "check_structured_env_vars",
    "E002_STRUCTURED_ENV_VAR",
    "W001_ENV_VAR_IGNORED",
]
