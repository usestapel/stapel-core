"""Guard over ``SILENCED_SYSTEM_CHECKS`` (tag ``stapel_check_guard``).

Django's ``SILENCED_SYSTEM_CHECKS`` is a blanket line: one list, any id, no
signal. Nothing in this fleet read it — the name appears in a dozen check
hints ("silence with SILENCED_SYSTEM_CHECKS if this deployment really is
...") and in no code at all, so a project could mute any library's security
check and leave no trace an operator, a reviewer or a gate could see. The
live example that motivated this module: a sandbox settings tier silencing
``stapel_auth.E001`` and ``stapel_auth.E004`` — and E004 exists for exactly
the combination being silenced.

Two mechanisms, and they are two halves of one contract:

**The marking lives with the check.** A library declares an id critical where
the id is *defined*::

    E004 = declare_security_critical(
        "stapel_auth.E004",
        "anonymous sessions combined with open registration",
    )

:func:`declare_security_critical` returns the id, so the constant IS the
declaration: there is no way to have the constant without it, and no separate
list anywhere that can drift out of step with the check that emits it.

**The finding refuses to go quiet.** The check emits
:class:`SecurityCriticalError` (or :class:`SecurityCriticalWarning`) instead
of ``checks.Error``. Those override ``is_silenced()`` so
``SILENCED_SYSTEM_CHECKS`` — the blanket line — does not apply to them.
Constructing one also declares its id, so the two halves cannot come apart in
the direction that matters (emitted as critical but not declared).

The escape hatch a project with a genuine reason needs is per-check,
explicit, and carries a written reason::

    STAPEL_SECURITY_CHECK_WAIVERS = {
        "stapel_auth.E004": "guests are the product here; registration is "
                            "invite-only, see ADR-114",
    }

One line per check, greppable by id, and W002 reports every active waiver at
every boot — a waiver is a stated exception, never a quiet one. An empty or
non-string reason is E002: the reason is the whole point.

Checks
------
E001  a security-critical id sits in ``SILENCED_SYSTEM_CHECKS`` with no
      waiver — the blanket route is refused for it.
E002  ``STAPEL_SECURITY_CHECK_WAIVERS`` is not a mapping of id → non-empty
      reason.
W001  what ``SILENCED_SYSTEM_CHECKS`` currently mutes, listed. Visibility, not
      judgement: silencing a non-critical check is a legitimate choice, and
      an invisible one until now.
W002  an active waiver, with its reason.
W003  a waiver for an id nothing declares security-critical — the waiver dict
      must not quietly become a second blanket list.
"""
from __future__ import annotations

from django.core import checks

#: Per-check escape hatch: ``{check_id: reason}``. Never a list of bare ids —
#: the reason is what makes the exception reviewable a year later.
WAIVERS_SETTING = "STAPEL_SECURITY_CHECK_WAIVERS"

E001_SECURITY_CHECK_SILENCED = "stapel_core.check_guard.E001"
E002_MALFORMED_WAIVERS = "stapel_core.check_guard.E002"
W001_CHECKS_SILENCED = "stapel_core.check_guard.W001"
W002_SECURITY_CHECK_WAIVED = "stapel_core.check_guard.W002"
W003_WAIVER_FOR_NON_CRITICAL = "stapel_core.check_guard.W003"

#: check id -> why the library considers it security-critical.
_SECURITY_CRITICAL: dict[str, str] = {}

#: What an auto-declaration (a SecurityCritical* message whose id was never
#: passed to declare_security_critical) records as its reason.
_UNDECLARED_REASON = "declared by the emitting check, not at its id constant"


def declare_security_critical(check_id: str, why: str) -> str:
    """Declare *check_id* security-critical and return it.

    Returns the id so the module-level constant is the declaration::

        E001_WEAK_SECRET = declare_security_critical(
            "stapel_core.prodguard.E001", "a placeholder production secret"
        )

    Re-declaring the same id is allowed (module re-import); the last *why*
    wins, which is the same string every time in practice.
    """
    if not check_id or not isinstance(check_id, str):
        raise ValueError("declare_security_critical needs a non-empty check id")
    if not why or not isinstance(why, str):
        raise ValueError(f"{check_id}: a security-critical declaration needs a reason")
    _SECURITY_CRITICAL[check_id] = why
    return check_id


def security_critical_ids() -> dict[str, str]:
    """Every declared security-critical id, mapped to its declared reason."""
    return dict(_SECURITY_CRITICAL)


def is_security_critical(check_id: str) -> bool:
    return check_id in _SECURITY_CRITICAL


def waivers() -> dict[str, str]:
    """The project's per-check waivers, ``{id: reason}``.

    Malformed entries are dropped here and reported by E002 — a waiver that
    cannot be read must not act as one, or a typo would silence a check while
    looking like a documented exception.
    """
    from django.conf import settings

    raw = getattr(settings, WAIVERS_SETTING, None) or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


class SecurityCriticalMessage:
    """Mixin: ``SILENCED_SYSTEM_CHECKS`` does not apply to this finding.

    Django's ``CheckMessage.is_silenced`` consults the blanket list; this one
    consults the per-check waivers instead. So the only route to quiet is the
    explicit one, and it is one line per check with a reason attached.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.id and self.id not in _SECURITY_CRITICAL:
            _SECURITY_CRITICAL[self.id] = _UNDECLARED_REASON

    def is_silenced(self) -> bool:
        return bool(self.id) and self.id in waivers()


class SecurityCriticalError(SecurityCriticalMessage, checks.Error):
    """An ``Error`` a blanket ``SILENCED_SYSTEM_CHECKS`` line cannot mute."""


class SecurityCriticalWarning(SecurityCriticalMessage, checks.Warning):
    """A ``Warning`` a blanket ``SILENCED_SYSTEM_CHECKS`` line cannot mute."""


def _silenced_ids() -> list[str]:
    from django.conf import settings

    raw = getattr(settings, "SILENCED_SYSTEM_CHECKS", None) or ()
    return sorted({str(entry) for entry in raw})


@checks.register("stapel_check_guard")
def check_silenced_system_checks(app_configs=None, **kwargs):
    """E001/E002/W001/W002/W003 — see the module docstring."""
    findings: list = []
    findings.extend(_waiver_shape_findings())

    silenced = _silenced_ids()
    critical = security_critical_ids()
    waived = waivers()

    unwaived_critical = [i for i in silenced if i in critical and i not in waived]
    for check_id in unwaived_critical:
        findings.append(SecurityCriticalError(
            f"SILENCED_SYSTEM_CHECKS contains {check_id!r}, which the library "
            f"that owns it declares security-critical ({critical[check_id]}). "
            f"A blanket silencing line is not an admissible route for it: the "
            f"list carries no reason, names no owner, and hides the finding "
            f"from every reader of `manage.py check` at once.",
            hint=f"If this deployment genuinely needs the exception, state it "
                 f"per check with a reason: {WAIVERS_SETTING} = "
                 f"{{{check_id!r}: 'why this deployment is different'}} — and "
                 f"remove {check_id!r} from SILENCED_SYSTEM_CHECKS. The waiver "
                 f"is reported at every boot, on purpose.",
            id=E001_SECURITY_CHECK_SILENCED,
        ))

    for check_id, reason in sorted(waived.items()):
        if check_id in critical:
            findings.append(checks.Warning(
                f"{WAIVERS_SETTING} waives the security-critical check "
                f"{check_id} ({critical[check_id]}). Stated reason: {reason}",
                hint="Waivers are reported at every boot so a temporary "
                     "exception cannot become forgotten configuration. Remove "
                     "the entry once the underlying finding is fixed.",
                id=W002_SECURITY_CHECK_WAIVED,
            ))
        else:
            findings.append(checks.Warning(
                f"{WAIVERS_SETTING} carries {check_id!r}, which no installed "
                f"library declares security-critical. The waiver does nothing: "
                f"an ordinary check is silenced by SILENCED_SYSTEM_CHECKS.",
                hint=f"Move {check_id!r} to SILENCED_SYSTEM_CHECKS, or drop it "
                     f"— it may be a stale id from a check that was renamed or "
                     f"removed. {WAIVERS_SETTING} must not become a second "
                     f"blanket list.",
                id=W003_WAIVER_FOR_NON_CRITICAL,
            ))

    ordinary = [i for i in silenced if i not in critical]
    if ordinary:
        findings.append(checks.Warning(
            "SILENCED_SYSTEM_CHECKS mutes "
            + ", ".join(repr(i) for i in ordinary)
            + ". Silencing a check is a legitimate choice and, until this "
              "finding existed, an invisible one: nothing in the framework "
              "read the setting, so any library's check could be muted with "
              "no signal to anybody.",
            hint="Nothing to fix if each id is a considered choice — this "
                 "finding exists so the choice is readable from `manage.py "
                 "check` instead of only from a settings module.",
            id=W001_CHECKS_SILENCED,
        ))
    return findings


def _waiver_shape_findings() -> list:
    from django.conf import settings

    raw = getattr(settings, WAIVERS_SETTING, None)
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [checks.Error(
            f"{WAIVERS_SETTING} must be a mapping of check id -> reason, "
            f"got {type(raw).__name__}. A bare list of ids is exactly the "
            f"blanket shape this setting exists to replace.",
            hint=f"{WAIVERS_SETTING} = {{'stapel_auth.E004': 'why this "
                 f"deployment is different'}}",
            id=E002_MALFORMED_WAIVERS,
        )]
    bad = [
        key for key, value in raw.items()
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip()
    ]
    if not bad:
        return []
    return [checks.Error(
        f"{WAIVERS_SETTING} entries with no usable reason: "
        + ", ".join(repr(key) for key in sorted(bad, key=repr))
        + ". A waiver without a reason is a blanket silencing line with extra "
          "steps; these entries do not waive anything.",
        hint="Give each entry a non-empty string saying why this deployment "
             "is the exception. The next reader has only that sentence.",
        id=E002_MALFORMED_WAIVERS,
    )]


__all__ = [
    "E001_SECURITY_CHECK_SILENCED",
    "E002_MALFORMED_WAIVERS",
    "W001_CHECKS_SILENCED",
    "W002_SECURITY_CHECK_WAIVED",
    "W003_WAIVER_FOR_NON_CRITICAL",
    "WAIVERS_SETTING",
    "SecurityCriticalError",
    "SecurityCriticalMessage",
    "SecurityCriticalWarning",
    "check_silenced_system_checks",
    "declare_security_critical",
    "is_security_critical",
    "security_critical_ids",
    "waivers",
]
