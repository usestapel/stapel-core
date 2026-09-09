"""System checks (tag ``stapel_drf_settings``) — a ``REST_FRAMEWORK`` key
this deployment sets must actually be in force.

:mod:`stapel_core.django.drf_rebind` repairs the DRF class attributes the
import-order trap left holding DRF's defaults. That repair is a mechanism,
and a mechanism that fails does so quietly — which is the exact property the
defect being fixed already had. So the outcome is checked, not the mechanism:
after ``ready()`` has run, every ``<cls>.<attr> = api_settings.<KEY>`` bind
derived from the *installed* DRF source is compared against the live
``api_settings`` value, and any disagreement is reported naming the class,
the attribute and the ``REST_FRAMEWORK`` key that is not being obeyed.

This cannot be satisfied by the repair merely running. It goes red when:

* a future DRF adds a class attribute nobody here has heard of, and the
  repair's derivation somehow missed it;
* something later in the boot — another library's ``ready()``, a project's
  own monkey-patch — writes a value over the configured one;
* an import string in ``REST_FRAMEWORK`` does not resolve, which DRF
  otherwise raises only at the first request that needs it.

Checks
------
E001  a class attribute DRF binds from ``api_settings`` does not hold the
      value this deployment configured. Error, because the whole symptom is
      silence: the setting is written, it reads back correctly from
      ``settings.REST_FRAMEWORK``, ``api_settings`` agrees with it, and the
      only thing that disagrees is the class every view inherits from. That
      is how a key can be set for months and do nothing. A deployment that
      really does mean to patch a DRF class attribute past its own setting
      names ``stapel_core.drf_settings.E001`` in ``SILENCED_SYSTEM_CHECKS``.
E002  ``REST_FRAMEWORK[<KEY>]`` cannot be resolved at all — a dotted path
      that does not import. DRF resolves import strings lazily, so without
      this the deployment boots green and raises inside the first request
      that touches the policy.
W001  DRF modules are imported whose source this process cannot read, so the
      bind set fell back to :data:`~stapel_core.django.drf_rebind.DECLARED_BINDS`
      and coverage of *those* modules is the floor rather than the truth.
      Warning: the floor is correct for every DRF this library was released
      against, and a source-stripped install is a packaging choice, not a
      misconfiguration.
"""
from __future__ import annotations

from django.core import checks

from stapel_core.django.drf_rebind import (
    DECLARED_BINDS,
    Bind,
    binds_to_repair,
    resolve,
    same_value,
)

E001_SETTING_NOT_IN_FORCE = "stapel_core.drf_settings.E001"
E002_SETTING_UNRESOLVABLE = "stapel_core.drf_settings.E002"
W001_SOURCE_UNREADABLE = "stapel_core.drf_settings.W001"


def _configured_keys() -> set[str]:
    """The ``REST_FRAMEWORK`` keys this deployment actually writes."""
    from django.conf import settings

    return set(getattr(settings, "REST_FRAMEWORK", None) or {})


def _describe(value) -> str:
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_describe(item) for item in value) + "]"
    return repr(value)


def _stale(bind: Bind, current, desired, configured: bool):
    written = (
        f"REST_FRAMEWORK['{bind.setting}'] is set by this deployment but "
        if configured
        else f"REST_FRAMEWORK['{bind.setting}'] resolves to a value that "
    )
    return checks.Error(
        f"{written}{bind.target} still holds {_describe(current)} instead of "
        f"{_describe(desired)}. DRF binds that attribute in its class body at "
        f"import time, so every view in this process inherits the stale value "
        f"— the setting reads back correctly everywhere except where it is "
        f"used.",
        hint="stapel_core.django.drf_rebind.rebind_api_settings() repairs "
             "these in CommonDjangoConfig.ready(); this attribute survived it, "
             "so something writes it afterwards, or 'stapel_core.django' is "
             "not in INSTALLED_APPS. If the overwrite is deliberate, silence "
             f"{E001_SETTING_NOT_IN_FORCE}.",
        id=E001_SETTING_NOT_IN_FORCE,
    )


@checks.register("stapel_drf_settings")
def check_rest_framework_settings_in_force(app_configs=None, **kwargs):
    """E001/E002/W001 — see the module docstring."""
    try:
        import rest_framework  # noqa: F401
        from rest_framework.settings import api_settings
    except Exception:  # pragma: no cover - DRF not installed
        return []

    findings: list = []
    configured = _configured_keys()
    binds, unreadable = binds_to_repair()

    for bind in binds:
        owner, desired, ok = resolve(bind)
        if owner is None:
            continue
        if not ok:
            # resolve() swallowed the lookup: either DRF dropped the setting
            # (nothing to enforce) or the deployment's import string is broken
            # (everything to say).
            if bind.setting not in configured:
                continue
            try:
                getattr(api_settings, bind.setting)
            except Exception as exc:
                findings.append(checks.Error(
                    f"REST_FRAMEWORK['{bind.setting}'] cannot be resolved: "
                    f"{type(exc).__name__}: {exc}. DRF imports that path "
                    f"lazily, so this is not a boot failure — it is an "
                    f"exception raised inside the first request that reaches "
                    f"{bind.target}.",
                    hint="Fix the dotted path, or remove the key and take "
                         "DRF's default.",
                    id=E002_SETTING_UNRESOLVABLE,
                ))
            continue

        if bind.attr not in owner.__dict__:
            continue
        current = owner.__dict__[bind.attr]
        if same_value(current, desired):
            continue
        findings.append(_stale(bind, current, desired, bind.setting in configured))

    if unreadable:
        findings.append(checks.Warning(
            f"The source of {len(unreadable)} imported DRF module(s) cannot be "
            f"read ({', '.join(sorted(unreadable))}), so the set of class "
            f"attributes DRF binds from REST_FRAMEWORK was taken from this "
            f"library's declared floor ({len(DECLARED_BINDS)} binds) instead "
            f"of from the installed DRF. A DRF newer than this release may "
            f"bind an attribute neither list knows about, and a setting for it "
            f"would be silently inert.",
            hint="Install DRF from a wheel or sdist that ships .py sources "
                 "(the default), rather than a source-stripped or zipped "
                 "distribution.",
            id=W001_SOURCE_UNREADABLE,
        ))

    return findings


__all__ = [
    "E001_SETTING_NOT_IN_FORCE",
    "E002_SETTING_UNRESOLVABLE",
    "W001_SOURCE_UNREADABLE",
    "check_rest_framework_settings_in_force",
]
