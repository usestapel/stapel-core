"""``SILENCED_SYSTEM_CHECKS`` stops being a blanket line nobody reads.

The setting is mentioned in a dozen check hints across the fleet ("silence
with SILENCED_SYSTEM_CHECKS if ...") and read by nothing: stapel-core,
stapel-auth, stapel-workspaces and stapel-tools all name it only in prose. Any
project could mute any library's security check with one line and leave no
signal for an operator, a reviewer or a gate. The live case that motivated
this: a sandbox tier silencing ``stapel_auth.E001`` and ``stapel_auth.E004``,
where E004 exists for exactly the combination being silenced.
"""
import pytest
from django.core import checks
from django.test import override_settings

from stapel_core.django.check_guard import (
    E001_SECURITY_CHECK_SILENCED,
    E002_MALFORMED_WAIVERS,
    W001_CHECKS_SILENCED,
    W002_SECURITY_CHECK_WAIVED,
    W003_WAIVER_FOR_NON_CRITICAL,
    WAIVERS_SETTING,
    SecurityCriticalError,
    SecurityCriticalWarning,
    check_silenced_system_checks,
    declare_security_critical,
    is_security_critical,
    security_critical_ids,
    waivers,
)

PROBE = "stapel_probe.E999"
ORDINARY = "stapel_probe.W111"


@pytest.fixture
def probe_declared():
    """A library declaring one of its ids security-critical, then undeclaring."""
    from stapel_core.django import check_guard

    declare_security_critical(PROBE, "the probe's stated reason")
    yield PROBE
    check_guard._SECURITY_CRITICAL.pop(PROBE, None)


def ids_of(findings):
    return [f.id for f in findings]


# ---------------------------------------------------------------------------
# The marking lives with the check
# ---------------------------------------------------------------------------


def test_the_declaration_returns_the_id_so_the_constant_is_the_marking(probe_declared):
    """`E = declare_security_critical("...", why)` — no second list to drift."""
    assert probe_declared == PROBE
    assert is_security_critical(PROBE)
    assert security_critical_ids()[PROBE] == "the probe's stated reason"


def test_a_declaration_without_a_reason_is_refused():
    with pytest.raises(ValueError):
        declare_security_critical("stapel_probe.E998", "")


def test_emitting_the_message_class_declares_the_id_too():
    """The two halves cannot come apart in the direction that matters:
    a finding emitted as security-critical is security-critical."""
    from stapel_core.django import check_guard

    try:
        SecurityCriticalError("boom", id="stapel_probe.E997")
        assert is_security_critical("stapel_probe.E997")
    finally:
        check_guard._SECURITY_CRITICAL.pop("stapel_probe.E997", None)


# ---------------------------------------------------------------------------
# The finding refuses to go quiet
# ---------------------------------------------------------------------------


def test_blanket_silencing_does_not_mute_a_security_critical_finding():
    """The mutant: drop `is_silenced` from SecurityCriticalMessage and this
    is the assertion that dies — a plain checks.Error here is silenced."""
    critical = SecurityCriticalError("no", id="stapel_probe.E996")
    ordinary = checks.Error("also no", id=ORDINARY)
    with override_settings(SILENCED_SYSTEM_CHECKS=["stapel_probe.E996", ORDINARY]):
        assert critical.is_silenced() is False
        assert ordinary.is_silenced() is True


def test_a_per_check_waiver_is_the_one_route_to_quiet():
    critical = SecurityCriticalWarning("no", id="stapel_probe.W996")
    with override_settings(**{WAIVERS_SETTING: {"stapel_probe.W996": "stated reason"}}):
        assert critical.is_silenced() is True


def test_a_waiver_without_a_reason_does_not_waive():
    """A blank reason is a blanket line with extra steps."""
    critical = SecurityCriticalError("no", id="stapel_probe.E995")
    with override_settings(**{WAIVERS_SETTING: {"stapel_probe.E995": "   "}}):
        assert critical.is_silenced() is False
        assert waivers() == {}


# ---------------------------------------------------------------------------
# The check that reports the silencing
# ---------------------------------------------------------------------------


def test_silencing_a_security_critical_check_is_an_error(probe_declared):
    with override_settings(SILENCED_SYSTEM_CHECKS=[PROBE]):
        findings = check_silenced_system_checks()
    assert E001_SECURITY_CHECK_SILENCED in ids_of(findings)
    finding = next(f for f in findings if f.id == E001_SECURITY_CHECK_SILENCED)
    assert finding.level >= checks.ERROR
    assert PROBE in finding.msg
    assert "the probe's stated reason" in finding.msg
    assert WAIVERS_SETTING in finding.hint


def test_the_error_names_the_per_check_route_out(probe_declared):
    """A gate with no admissible exit gets the whole tag silenced instead."""
    with override_settings(
        SILENCED_SYSTEM_CHECKS=[PROBE],
        **{WAIVERS_SETTING: {PROBE: "this deployment is genuinely different"}},
    ):
        findings = check_silenced_system_checks()
    assert E001_SECURITY_CHECK_SILENCED not in ids_of(findings)
    assert W002_SECURITY_CHECK_WAIVED in ids_of(findings)


def test_an_active_waiver_reports_itself_with_its_reason(probe_declared):
    with override_settings(**{WAIVERS_SETTING: {PROBE: "ADR-114 says so"}}):
        findings = check_silenced_system_checks()
    waived = next(f for f in findings if f.id == W002_SECURITY_CHECK_WAIVED)
    assert "ADR-114 says so" in waived.msg


def test_ordinary_silencing_is_reported_but_not_refused():
    with override_settings(SILENCED_SYSTEM_CHECKS=[ORDINARY]):
        findings = check_silenced_system_checks()
    assert ids_of(findings) == [W001_CHECKS_SILENCED]
    assert findings[0].level < checks.ERROR
    assert ORDINARY in findings[0].msg


def test_nothing_silenced_is_no_finding():
    with override_settings(SILENCED_SYSTEM_CHECKS=[]):
        assert check_silenced_system_checks() == []


def test_a_waiver_for_a_non_critical_id_is_reported_as_pointless():
    """The waiver dict must not quietly become a second blanket list."""
    with override_settings(**{WAIVERS_SETTING: {ORDINARY: "why not"}}):
        findings = check_silenced_system_checks()
    assert W003_WAIVER_FOR_NON_CRITICAL in ids_of(findings)


def test_a_waiver_list_instead_of_a_mapping_is_an_error():
    with override_settings(**{WAIVERS_SETTING: ["stapel_auth.E004"]}):
        findings = check_silenced_system_checks()
    assert E002_MALFORMED_WAIVERS in ids_of(findings)


def test_a_waiver_with_an_empty_reason_is_an_error(probe_declared):
    with override_settings(**{WAIVERS_SETTING: {PROBE: ""}}):
        findings = check_silenced_system_checks()
    bad = next(f for f in findings if f.id == E002_MALFORMED_WAIVERS)
    assert PROBE in bad.msg


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_guard_is_on_the_boot_gate_roster():
    """Under gunicorn nothing else runs checks — which is exactly where a
    settings module full of silencing lines is deployed."""
    from stapel_core.django.boot import BOOT_GATE_TAGS

    assert "stapel_check_guard" in BOOT_GATE_TAGS


def test_core_declares_its_own_security_critical_ids():
    """Dogfooding: the mechanism is worth nothing if core's own gates are
    still mutable by one blanket line. These four were, until now — including
    the CORS pair that reproduces an audited cross-origin read in Python, and
    the auth backend that turns password login into 'any nonempty string'."""
    from stapel_core.django import (
        auth_backend_checks,
        blacklist_checks,
        cors_checks,
        mandate,
        prodguard,
    )

    declared = security_critical_ids()
    for check_id in (
        prodguard.E001_WEAK_SECRET,
        prodguard.E002_WEAK_DB_PASSWORD,
        mandate.E001_MANDATE_SEAM_UNREACHABLE,
        cors_checks.E001_CREDENTIALS_WITH_ALL_ORIGINS,
        auth_backend_checks.E003_DOES_NOT_VERIFY_CREDENTIALS,
        blacklist_checks.W001_BLACKLIST_FAIL_OPEN,
    ):
        assert check_id in declared
        assert declared[check_id] and len(declared[check_id]) > 20


def test_the_marked_core_checks_emit_the_unsilenceable_class():
    """Declaring an id and then emitting a plain checks.Error is half a
    contract: the id would be listed as critical and the finding would still
    go quiet on a blanket line."""
    from django.test import override_settings as _override

    from stapel_core.django.blacklist_checks import (
        W001_BLACKLIST_FAIL_OPEN,
        check_blacklist_fail_open,
    )
    from stapel_core.django.cors_checks import (
        E001_CREDENTIALS_WITH_ALL_ORIGINS,
        check_cors_credentials,
    )

    with _override(CORS_ALLOW_ALL_ORIGINS=True, CORS_ALLOW_CREDENTIALS=True):
        cors = next(
            f for f in check_cors_credentials()
            if f.id == E001_CREDENTIALS_WITH_ALL_ORIGINS
        )
    with _override(STAPEL_BLACKLIST_FAIL_OPEN=True):
        hatch = check_blacklist_fail_open()[0]

    with override_settings(SILENCED_SYSTEM_CHECKS=[
        E001_CREDENTIALS_WITH_ALL_ORIGINS, W001_BLACKLIST_FAIL_OPEN,
    ]):
        assert cors.is_silenced() is False
        assert hatch.is_silenced() is False
