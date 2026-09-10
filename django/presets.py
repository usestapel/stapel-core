"""Named deployment postures (tag ``stapel_presets``) — values plus the check
that keeps them true.

A posture is the handful of settings that decide *what kind of installation
this is*: does a person off the street get an account, and does that account
get a mandate. Until now the fleet had no artifact for it. A client's stand
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

Stage: a deployment can be production and not yet launched
----------------------------------------------------------
A stand can be run as production — public host, real TLS, real data — while
nobody has been told about it yet. On such a stand a mock channel is a
deliberate choice, not an oversight, and the only way to keep it was to
silence the library check that reports it: ``SILENCED_SYSTEM_CHECKS`` erases
the finding and records no intent, so the next reader cannot tell a decision
from a leftover.

So the posture carries a ``stage``: ``"live"`` (the default) or
``"prototype"``. It changes two things and nothing else.

* The ``prototype`` spread says **nothing** about the mock keys — they are
  absent from it, so the deployment sets them freely and
  :func:`check_posture_coherence` has nothing to compare. In ``live`` they
  stay pinned off and security-relevant, as before.
* A library check that produced an Error about a mock/console/stub channel
  passes it through :func:`stage_finding`, which downgrades it to a Warning
  carrying the same message plus the sentence that says why it is expected —
  in the ``prototype`` stage only. Undeclared or ``live``: unchanged Error.

The stage is a value in the deployment's own settings file and in the
declared manifest, so "this is a prototype" is greppable, is reported at
every boot, and stops being true the moment somebody flips it. W003 keeps it
from outliving its reason.

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
W003  the ``prototype`` stage is declared and nothing prototypical is on.

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
W003_PROTOTYPE_STAGE_IDLE = "stapel_core.presets.W003"

#: Admissible values of the ``stage`` option. ``live`` is the default for the
#: same reason ``invite_only`` is: the relaxed one has to be chosen by name.
STAGES = ("live", "prototype")

#: Appended to a finding a library check produced about a stub channel when
#: the declared stage is ``prototype``. One sentence, always the same one, so
#: a log reader learns it once.
PROTOTYPE_STAGE_NOTE = (
    "Declared posture stage is prototype: this is expected until launch. "
    'Flip the posture to stage="live" before the deployment is advertised; '
    "the same finding is then an error."
)

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


def _validate_stage(stage: str) -> str:
    if stage not in STAGES:
        raise ValueError(
            f"stage={stage!r} — admissible stages: {', '.join(STAGES)}"
        )
    return stage


# The floor both postures stand on: values that are wrong in every named
# deployment, private or public. One place, so a new preset cannot forget them.
#
# In the prototype stage the mock keys are OMITTED rather than set: a posture
# that pinned them on would be asserting something about a deployment it knows
# nothing about, and one that pinned them off would be the thing this stage
# exists to avoid. Absent means the deployment decides and the coherence check
# has nothing to compare.
def _floor(*, stage: str = "live") -> dict[str, dict[str, PresetValue]]:
    _validate_stage(stage)
    if stage != "live":
        return {"STAPEL_AUTH": {}}
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


def _private_spec(
    *, door: str = "invite_only", stage: str = "live",
) -> dict[str, dict[str, PresetValue]]:
    if door not in PRIVATE_DOORS:
        raise ValueError(
            f"private_space(door={door!r}) — admissible doors: "
            f"{', '.join(PRIVATE_DOORS)}"
        )
    spec = _floor(stage=stage)
    spec["STAPEL_WORKSPACES"] = {
        "STREET_LANDING_MODE": PresetValue(
            "none",
            "the axis private space IS: a street signup mints no mandate, so "
            "an account off the street is harmless rather than forbidden",
            security_relevant=True,
        ),
    }
    # The requests door opens ONE method, explicitly, in the returned values —
    # so "this deployment lets anyone register" is a line in the deployment's
    # settings rather than a default nobody chose.
    spec["STAPEL_AUTH"].update(_registration(
        EMAIL=(door == "requests"),
        PHONE=False,
        OAUTH=False,
        SSO=False,
        PASSWORD=False,
    ))
    return spec


def _public_spec(*, stage: str = "live") -> dict[str, dict[str, PresetValue]]:
    spec = _floor(stage=stage)
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


def private_space(*, door: str = "invite_only", stage: str = "live") -> dict[str, dict]:
    """A private cloud: registration closed, and an account that gets in holds
    no mandate until the owner grants one.

    ``door`` selects the one sanctioned way a stranger may still create an
    account: ``"invite_only"`` (none — the default) or ``"requests"``, which
    opens email registration so a visitor can ask, and only ask.

    ``stage`` says whether this deployment is launched: ``"live"`` (the
    default) or ``"prototype"``, which leaves the mock-channel keys out of the
    spread and turns the library findings about them into warnings that say
    so. See the module docstring.

    Returns ``{"STAPEL_POSTURE": ..., "STAPEL_AUTH": {...},
    "STAPEL_WORKSPACES": {...}}`` for the settings module to spread. It
    imports no module and returns no code: a posture is a composition of keys,
    which is why it can live in the core without the core depending on the
    modules that own them.
    """
    return _flatten(
        "private_space",
        {"door": door, "stage": stage},
        _private_spec(door=door, stage=stage),
    )


def public_space(*, stage: str = "live") -> dict[str, dict]:
    """A public cloud: registration open on every address-verified method, and
    a street signup lands in a personal workspace.

    The sibling of :func:`private_space`, and the reason the private default is
    safe: the two postures differ by KIND, so "open" is something a deployment
    picks by name rather than something it inherits by forgetting. ``stage``
    is the same option it is there.
    """
    return _flatten("public_space", {"stage": stage}, _public_spec(stage=stage))


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


def stage() -> str | None:
    """The stage this deployment declares, or ``None`` when no posture is.

    ``None`` and ``"live"`` are different answers to different questions —
    "nobody said" versus "somebody said launched" — and a caller that only
    wants to know whether relaxations are sanctioned treats them the same.
    A posture declared without the option reads as ``"live"``: a manifest
    that predates the option cannot thereby claim to be a prototype.
    """
    declared = declared_posture()
    if declared is None:
        return None
    _, options = declared
    value = options.get("stage", "live")
    return value if value in STAGES else "live"


def stage_finding(
    finding: checks.CheckMessage, *, warning_id: str,
) -> checks.CheckMessage:
    """An Error about a stub channel, as this deployment's stage reports it.

    Pass an Error a check has already built about a mock/console/stub
    channel. In the ``live`` stage and with no posture declared it comes back
    unchanged. In the ``prototype`` stage it comes back as a
    :class:`~django.core.checks.Warning` under *warning_id* — same message
    plus :data:`PROTOTYPE_STAGE_NOTE`, same hint, same object.

    The id mapping belongs to the caller: the error id travels on the finding
    and the warning id is named at the call site, so the pair is greppable in
    the module that owns both. This helper only decides which one applies.
    """
    if stage() != "prototype":
        return finding
    return checks.Warning(
        f"{finding.msg} {PROTOTYPE_STAGE_NOTE}",
        hint=finding.hint,
        obj=finding.obj,
        id=warning_id,
    )


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


#: Keys whose truth means "a channel here is stubbed". Namespaced, read
#: through :func:`_effective` like every other posture value.
_STUB_CHANNEL_KEYS = (
    ("STAPEL_AUTH", "USE_MOCK_SMS_OTP"),
    ("STAPEL_AUTH", "USE_MOCK_EMAIL_OTP"),
)

#: Substrings of an ``EMAIL_BACKEND`` that delivers nowhere.
_STUB_EMAIL_BACKENDS = ("console", "locmem", "dummy", "filebased")


def _anything_is_stubbed() -> bool:
    """Is any channel on this deployment a stub?"""
    for namespace, key in _STUB_CHANNEL_KEYS:
        value = _effective(namespace, key)
        if value is not _ABSENT and value:
            return True

    from django.conf import settings

    backend = str(getattr(settings, "EMAIL_BACKEND", "") or "").lower()
    return any(name in backend for name in _STUB_EMAIL_BACKENDS)


def _idle_prototype_findings() -> list:
    """W003 — the prototype stage declared over nothing prototypical.

    A prototype stage that runs on real channels costs the deployment
    nothing today and everything on the day a mock is switched back on: the
    stage is already there to excuse it. It is a Warning because a stand
    between "stubs off" and "stage flipped" is a legitimate half-hour, not a
    defect — the finding exists so the stage does not outlive its reason.

    DEBUG=False and a public ALLOWED_HOSTS are NOT part of this: a prototype
    IS production but unadvertised, and reporting that combination would be
    reporting the whole point of the stage.
    """
    if _anything_is_stubbed():
        return []
    return [checks.Warning(
        'The declared posture stage is "prototype", but nothing prototypical '
        "is on: no mock one-time-code channel is enabled and the email "
        "backend delivers for real. Either flip to live or say what is "
        "stubbed.",
        hint='Set stage="live" in the preset call now that the deployment '
             "runs on real channels — the stage is what turns the library's "
             "findings about stub channels into warnings, and one left "
             "behind will excuse the next mock switched on by accident.",
        id=W003_PROTOTYPE_STAGE_IDLE,
    )]


@checks.register("stapel_presets")
def check_posture_coherence(app_configs=None, **kwargs):
    """E001/E002/W001/W003 — declaration and behaviour agree.

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

    if stage() == "prototype":
        findings.extend(_idle_prototype_findings())

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
    "W003_PROTOTYPE_STAGE_IDLE",
    "POSTURE_SETTING",
    "PROTOTYPE_STAGE_NOTE",
    "RETIRED_ENV_SETTING",
    "PRESETS",
    "PRIVATE_DOORS",
    "STAGES",
    "PresetValue",
    "check_posture_coherence",
    "declared_posture",
    "posture_spec",
    "private_space",
    "public_space",
    "stage",
    "stage_finding",
]
