"""Named deployment postures (tag ``stapel_presets``) — values plus the check
that keeps them true.

A posture is the handful of settings that decide *what kind of installation
this is*: does a person off the street get an account, and does that account
get a mandate. Until now the fleet had no artifact for it. meettoday's stand
carried its posture as a bespoke settings tier — sixty-five lines of product
code that re-read the mock-OTP flags from the environment, defaulting them
**on**, over a production layer that pinned them off, and silenced the two
auth checks that report exactly that combination. It was not wrong by
accident: nothing named the posture, so there was nothing to reuse and nothing
to contradict.

Two halves, and neither is useful alone
---------------------------------------
**(a) A generator of explicit values.** :func:`private_space` /
:func:`public_space` return per-namespace dicts a settings module *spreads*::

    from stapel_core.django.presets import private_space

    _preset = private_space(door="requests")
    STAPEL_POSTURE = _preset["STAPEL_POSTURE"]
    STAPEL_AUTH = {**STAPEL_AUTH, **_preset["STAPEL_AUTH"]}
    STAPEL_WORKSPACES = {**STAPEL_WORKSPACES, **_preset["STAPEL_WORKSPACES"]}

Values, not runtime indirection: they land in settings at settings-definition
time, so the fleet rule "an explicit value always wins" stays literal and an
override is a greppable line *below* the spread. There is no preset object
resolving anything per request.

**(b) A coherence check.** A preset alone is a snapshot, and a snapshot
without a drift gate goes stale silently — the product overrides one line, the
posture comes apart, and the name stays. So the invariant does not live in the
generator: :func:`check_posture_coherence` re-derives the posture from
``STAPEL_POSTURE`` (which records the preset NAME and its options, never the
values — a manifest that carried values could be edited to launder an
override) and compares it against what the deployment actually runs.

The comparison asks the running namespace, not the literal dict, whenever the
module is installed here: ``AppSettings`` applies env layering, and a posture
that an environment variable can undo is not a posture. Overriding remains
allowed — a private cloud fronted by a corporate IdP legitimately reopens
``AUTH_SSO_REGISTRATION`` — but a security-relevant override is never
*silent*: it is a :class:`~stapel_core.django.check_guard.SecurityCriticalError`
that ``SILENCED_SYSTEM_CHECKS`` cannot mute, and the only route to quiet is
``STAPEL_SECURITY_CHECK_WAIVERS = {"stapel_core.presets.E001": "why"}``, which
is reported at every boot with its reason.

What a preset does not contain
------------------------------
Secrets and environment addresses (``ALLOWED_HOSTS``, hosts, URLs, provider
credentials — those are the deployment's, and a posture that shipped them
would be wrong on its second consumer), ``INSTALLED_APPS`` (topology, not
posture), and any value it cannot justify: every key carries its reason in
:class:`PresetValue`, and a preset of settings-just-in-case is a design
document in Python.

Retired environment variables
-----------------------------
Adopting a posture usually stops some environment variable from being read —
and the variable stays in every stand's ``.env``, looking live, telling the
operator a flag still does something. ``STAPEL_RETIRED_ENV = {name: why}``
declares those names; W002 reports each one that is actually set here. It is
the same idea as ``stapel_core.conf.W001`` (set-but-ignored variables on
``no_env`` keys), for the names a *product* used to read in its own settings
module, which no library naming convention can find.

Checks
------
E001  a security-relevant posture value is not what the declared preset says
      (``SecurityCriticalError``: waivable per id, never silenceable).
E002  ``STAPEL_POSTURE`` is malformed, names no known preset, or carries
      options the preset refuses.
W001  a posture value that is not security-relevant differs — visibility, not
      judgement.
W002  a declared retired environment variable is set on this deployment and
      nothing reads it.

What this does not catch
------------------------
* **Namespaces this process does not install.** In a split deployment the
  auth service has no ``STAPEL_WORKSPACES`` and the check can only read the
  raw dict the settings file spread; if the fragment was never spread there,
  the finding says so, but nothing here reaches across processes. The fleet
  gate is the cross-service place for that.
* **Whether the posture is the right one.** Nothing here reasons about
  whether a deployment should be private; it reports that what is declared
  and what runs have come apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from django.core import checks

from stapel_core.django.check_guard import (
    SecurityCriticalError,
    declare_security_critical,
)

#: The setting a project assigns from a preset's ``STAPEL_POSTURE`` entry. It
#: records the preset's NAME and OPTIONS only — the values are re-derived, so
#: this dict cannot be edited into agreement with a drifted setting.
POSTURE_SETTING = "STAPEL_POSTURE"

E001_POSTURE_VALUE_OVERRIDDEN = declare_security_critical(
    "stapel_core.presets.E001",
    "a security-relevant value of the declared deployment posture (registration"
    " doors, mandate-granting landing mode, mock one-time codes)",
)
E002_BAD_POSTURE_DECLARATION = "stapel_core.presets.E002"
W001_POSTURE_VALUE_DIFFERS = "stapel_core.presets.W001"
W002_RETIRED_ENV_SET = "stapel_core.presets.W002"

#: ``{env var name: why nothing reads it any more}``. Declared by the project
#: whose settings module stopped reading them.
RETIRED_ENV_SETTING = "STAPEL_RETIRED_ENV"

_ABSENT = object()


@dataclass(frozen=True)
class PresetValue:
    """One posture value, its reason, and whether an override is security news.

    ``security_relevant`` is not "this setting is about security" — it is
    "changing it away from the posture weakens the posture". It decides
    between E001 and W001, i.e. between a finding a deploy gate stops on and
    one it prints.
    """

    value: Any
    why: str
    security_relevant: bool = False


# The floor both postures stand on: values that are wrong in every named
# deployment, private or public. One place, so a new preset cannot forget them.
def _floor() -> dict[str, dict[str, PresetValue]]:
    return {
        "STAPEL_AUTH": {
            "USE_MOCK_SMS_OTP": PresetValue(
                False,
                "a fixed passcode accepted for any address is not a demo "
                "convenience: the attacker does not sign up as himself, he "
                "authenticates as an existing owner's address",
                security_relevant=True,
            ),
            "USE_MOCK_EMAIL_OTP": PresetValue(
                False,
                "same hazard on the channel every account actually uses; "
                "stapel_auth.E001/E004 report exactly this pair",
                security_relevant=True,
            ),
        },
    }


def _registration(**doors: bool) -> dict[str, PresetValue]:
    """The five registration gates, each stated rather than inherited.

    Stating a default is the point: the posture must not change under the
    library's feet when a default does, and a reader of the settings file must
    be able to see which doors this installation opened.
    """
    why_open = "this posture ships registration open — the door is the posture"
    why_shut = (
        "private ships registration closed: entry is the owner's decision, "
        "not the visitor's"
    )
    return {
        f"AUTH_{name}_REGISTRATION": PresetValue(
            open_,
            why_open if open_ else why_shut,
            security_relevant=True,
        )
        for name, open_ in doors.items()
    }


#: Doors :func:`private_space` knows. ``invite_only`` is the default because a
#: private cloud that shipped an open door by default would be one forgotten
#: argument away from being public.
PRIVATE_DOORS = ("invite_only", "requests")


def _private_spec(*, door: str = "invite_only") -> dict[str, dict[str, PresetValue]]:
    if door not in PRIVATE_DOORS:
        raise ValueError(
            f"private_space(door={door!r}) — admissible doors: "
            f"{', '.join(PRIVATE_DOORS)}"
        )
    spec = _floor()
    spec["STAPEL_WORKSPACES"] = {
        "STREET_LANDING_MODE": PresetValue(
            "none",
            "the axis private space IS: a street signup mints no mandate, so "
            "an account off the street is harmless rather than forbidden",
            security_relevant=True,
        ),
    }
    # The requests door opens ONE method, explicitly, in the returned values —
    # so "meettoday lets anyone register" is a line in the deployment's
    # settings rather than a default nobody chose.
    spec["STAPEL_AUTH"].update(_registration(
        EMAIL=(door == "requests"),
        PHONE=False,
        OAUTH=False,
        SSO=False,
        PASSWORD=False,
    ))
    return spec


def _public_spec() -> dict[str, dict[str, PresetValue]]:
    spec = _floor()
    spec["STAPEL_WORKSPACES"] = {
        "STREET_LANDING_MODE": PresetValue(
            "personal",
            "the open shape: a street signup lands in a workspace of its own",
        ),
    }
    spec["STAPEL_AUTH"].update(_registration(
        EMAIL=True, PHONE=True, OAUTH=True, SSO=True, PASSWORD=False,
    ))
    # Password registration stays shut in BOTH postures: a self-chosen password
    # is not an address anybody verified, and no product in the fleet wants it
    # as the street door. A deployment that does say so below the spread (W001).
    return spec


def _flatten(name: str, options: Mapping[str, Any],
             spec: Mapping[str, Mapping[str, PresetValue]]) -> dict[str, dict]:
    values: dict[str, dict] = {
        namespace: {key: item.value for key, item in entries.items()}
        for namespace, entries in spec.items()
    }
    values[POSTURE_SETTING] = {"PRESET": name, "OPTIONS": dict(options)}
    return values


def private_space(*, door: str = "invite_only") -> dict[str, dict]:
    """A private cloud: registration closed, and an account that gets in holds
    no mandate until the owner grants one.

    ``door`` selects the one sanctioned way a stranger may still create an
    account: ``"invite_only"`` (none — the default) or ``"requests"``, which
    opens email registration so a visitor can ask, and only ask.

    Returns ``{"STAPEL_POSTURE": ..., "STAPEL_AUTH": {...},
    "STAPEL_WORKSPACES": {...}}`` for the settings module to spread. It
    imports no module and returns no code: a posture is a composition of keys,
    which is why it can live in the core without the core depending on the
    modules that own them.
    """
    return _flatten("private_space", {"door": door}, _private_spec(door=door))


def public_space() -> dict[str, dict]:
    """A public cloud: registration open on every address-verified method, and
    a street signup lands in a personal workspace.

    The sibling of :func:`private_space`, and the reason the private default is
    safe: the two postures differ by KIND, so "open" is something a deployment
    picks by name rather than something it inherits by forgetting.
    """
    return _flatten("public_space", {}, _public_spec())


#: Preset name → spec builder. The check re-derives from here, so a preset that
#: is not listed cannot be declared (E002).
PRESETS: dict[str, Callable[..., dict[str, dict[str, PresetValue]]]] = {
    "private_space": _private_spec,
    "public_space": _public_spec,
}


def posture_spec(name: str, **options: Any) -> dict[str, dict[str, PresetValue]]:
    """The annotated spec (values + reasons) of preset *name*.

    The documentation half of the artifact: every key a posture sets, with the
    sentence that justifies it, without going through a settings module.
    """
    try:
        builder = PRESETS[name]
    except KeyError:
        raise ValueError(
            f"unknown preset {name!r} — known: {', '.join(sorted(PRESETS))}"
        ) from None
    return builder(**options)


def declared_posture() -> tuple[str, dict] | None:
    """``(preset name, options)`` this deployment declares, or ``None``.

    ``None`` means no preset is in use, and the check stays quiet: a project
    that never adopted a posture is not thereby incoherent.
    """
    from django.conf import settings

    raw = getattr(settings, POSTURE_SETTING, None)
    if not raw:
        return None
    if not isinstance(raw, dict):
        return ("", {})
    name = raw.get("PRESET")
    options = raw.get("OPTIONS") or {}
    if not isinstance(name, str) or not isinstance(options, dict):
        return ("", {})
    return (name, options)


def _effective(namespace: str, key: str) -> Any:
    """What this process will actually read for ``namespace[key]``.

    The owning module's ``AppSettings`` instance is asked first when it is
    installed here: it applies env layering and defaults, so a posture value an
    environment variable quietly reopened is still caught. The raw settings
    dict is the fallback — a split deployment that only spreads the fragment
    has no instance to ask, and reading the dict is reading this deployment's
    own settings, never importing another module.
    """
    from stapel_core.conf import registered_settings

    for instance in registered_settings():
        if getattr(instance, "namespace", None) == namespace:
            try:
                return getattr(instance, key)
            except Exception:  # pragma: no cover - unknown key on that instance
                break

    from django.conf import settings

    raw = getattr(settings, namespace, None)
    if not isinstance(raw, dict):
        return _ABSENT
    return raw.get(key, _ABSENT)


def _shown(value: Any) -> str:
    return "not set at all" if value is _ABSENT else repr(value)


def _retired_env_findings() -> list:
    """W002 — declared-retired variables that this deployment still sets.

    Only the NAME is reported, never the value: a retired variable can be a
    credential for the mechanism that was retired.
    """
    import os

    from django.conf import settings

    declared = getattr(settings, RETIRED_ENV_SETTING, None) or {}
    if not isinstance(declared, dict):
        return [checks.Error(
            f"{RETIRED_ENV_SETTING} must be a mapping of env var name -> why "
            f"nothing reads it, got {type(declared).__name__}.",
            hint=f"{RETIRED_ENV_SETTING} = {{'AUTH_USE_MOCK_EMAIL_OTP': 'the "
                 f"private posture pins mock OTP off'}}",
            id=E002_BAD_POSTURE_DECLARATION,
        )]
    return [
        checks.Warning(
            f"{name} is set in this deployment's environment and nothing "
            f"reads it: {why}",
            hint="Remove the variable from the deployment's .env. Until then "
                 "it reads as live configuration to everyone who opens that "
                 "file, and its apparent effect is not the running one.",
            id=W002_RETIRED_ENV_SET,
        )
        for name, why in sorted(declared.items())
        if isinstance(name, str) and name in os.environ
    ]


@checks.register("stapel_presets")
def check_posture_coherence(app_configs=None, **kwargs):
    """E001/E002/W001 — what the deployment declares and what it runs agree.

    Independent of how a value arrived: the check reads the effective setting,
    so a hand-written line below the spread, a namespace that was never spread
    and an environment variable are the same finding.
    """
    from django.conf import settings

    findings: list = _retired_env_findings()
    declared = declared_posture()
    if declared is None:
        return findings
    name, options = declared
    if not name:
        findings.append(checks.Error(
            f"{POSTURE_SETTING} must be {{'PRESET': <name>, 'OPTIONS': {{...}}}} "
            f"as returned by a preset, got "
            f"{getattr(settings, POSTURE_SETTING, None)!r}.",
            hint="Assign it from the preset itself: "
                 "STAPEL_POSTURE = _preset['STAPEL_POSTURE'] — the manifest is "
                 "the preset's own output, never hand-written.",
            id=E002_BAD_POSTURE_DECLARATION,
        ))
        return findings
    try:
        spec = posture_spec(name, **options)
    except (ValueError, TypeError) as exc:
        findings.append(checks.Error(
            f"{POSTURE_SETTING} declares preset {name!r} with options "
            f"{options!r}, which the preset refuses: {exc}",
            hint="Known presets: " + ", ".join(sorted(PRESETS)) + ". The "
                 "declaration is generated by the preset call — a mismatch "
                 "means it was edited by hand or the preset changed shape.",
            id=E002_BAD_POSTURE_DECLARATION,
        ))
        return findings

    for namespace, entries in spec.items():
        for key, item in entries.items():
            live = _effective(namespace, key)
            if live == item.value and type(live) is type(item.value):
                continue
            message = (
                f"{namespace}['{key}'] is {_shown(live)}, but this deployment "
                f"declares the {name} posture, which sets it to "
                f"{item.value!r} — {item.why}."
            )
            if item.security_relevant:
                findings.append(SecurityCriticalError(
                    message,
                    hint=f"Either drop the override (the preset spread is the "
                         f"line that should win) or keep it and say why: "
                         f"STAPEL_SECURITY_CHECK_WAIVERS = "
                         f"{{{E001_POSTURE_VALUE_OVERRIDDEN!r}: 'why this "
                         f"deployment is different'}}. A posture may be "
                         f"departed from; it may not be departed from quietly.",
                    id=E001_POSTURE_VALUE_OVERRIDDEN,
                ))
            else:
                findings.append(checks.Warning(
                    message,
                    hint="Nothing to fix if the override is deliberate — this "
                         "finding exists so the posture's name and the "
                         "deployment's behaviour cannot disagree unnoticed.",
                    id=W001_POSTURE_VALUE_DIFFERS,
                ))
    return findings


__all__ = [
    "E001_POSTURE_VALUE_OVERRIDDEN",
    "E002_BAD_POSTURE_DECLARATION",
    "W001_POSTURE_VALUE_DIFFERS",
    "W002_RETIRED_ENV_SET",
    "POSTURE_SETTING",
    "RETIRED_ENV_SETTING",
    "PRESETS",
    "PRIVATE_DOORS",
    "PresetValue",
    "check_posture_coherence",
    "declared_posture",
    "posture_spec",
    "private_space",
    "public_space",
]
