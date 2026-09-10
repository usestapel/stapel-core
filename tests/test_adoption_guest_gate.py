"""W003 — the guest question asked of the gate instead of its spelling.

E001 reads a view as having taken a position as soon as any second permission
class stands beside ``IsAuthenticated``. Measured in a library, 2026-09-10:
five user-facing views gated on ``[IsAuthenticated, <a business gate>]``
admitted guest sessions while the check stayed silent, and the only two views
it did report were the ones without a companion class. A name in a list
cannot say whether a class asks about identity; running it can.
"""
import pytest
from django.core import checks as django_checks
from django.test import override_settings

from stapel_core.django.adoption_checks import (
    E001_ANONYMOUS_STANCE_UNDECLARED,
    W003_GUEST_ADMITTED_BY_THE_GATE,
    GuestPrincipal,
    check_anonymous_stance_declared,
    check_guest_admitted_by_the_gate,
    gate_admits,
)

URLS = "tests.adoption_guest_gate_urls"
STATIC_URLS = "tests.adoption_anonymous_urls"


@pytest.fixture
def axis_on(monkeypatch):
    """stapel_auth is not installed in core's own test env — the axis lives
    in the auth module, so the premise is forced here."""
    monkeypatch.setattr(
        "stapel_core.django.adoption_checks.anonymous_axis_enabled", lambda: True
    )


def _reported(findings):
    return sorted(
        f.msg.split(" ", 1)[0].rsplit(".", 1)[-1]
        for f in findings
        if f.id == W003_GUEST_ADMITTED_BY_THE_GATE
    )


# ---------------------------------------------------------- the three routes


@pytest.mark.usefixtures("axis_on")
class TestTheGateIsRun:
    @override_settings(ROOT_URLCONF=URLS)
    def test_a_guest_admitted_with_nothing_said_is_reported(self):
        """The defect: a business gate beside IsAuthenticated, and a guest
        walks through it."""
        findings = check_guest_admitted_by_the_gate()
        assert "BusinessGatedView" in _reported(findings)
        [warning] = [
            f for f in findings if "BusinessGatedView" in f.msg
        ]
        assert isinstance(warning, django_checks.Warning)
        assert warning.id == W003_GUEST_ADMITTED_BY_THE_GATE
        assert "refuses an unauthenticated caller and admits a guest" in warning.msg
        assert "[IsAuthenticated, NotClosing]" in warning.msg
        assert "/api/business/" in warning.msg
        # The finding is "say which you meant", never "close the view".
        assert "IsNotAnonymousUser" in warning.hint
        assert "ANONYMOUS_ALLOWED" in warning.hint

    @override_settings(ROOT_URLCONF=URLS)
    def test_a_gate_that_keeps_guests_out_is_silent(self):
        reported = _reported(check_guest_admitted_by_the_gate())
        assert "IdentityGatedView" not in reported
        assert "RefusesBothView" not in reported

    @override_settings(ROOT_URLCONF=URLS)
    def test_a_view_that_admits_everyone_is_simply_public(self):
        """The guest axis says nothing about a view an unauthenticated
        caller already reaches."""
        assert "PublicView" not in _reported(check_guest_admitted_by_the_gate())

    @override_settings(ROOT_URLCONF=URLS)
    def test_a_declared_stance_is_green_own_or_inherited(self):
        reported = _reported(check_guest_admitted_by_the_gate())
        assert "DeclaredBusinessGatedView" not in reported
        assert "InheritedDeclarationView" not in reported

    @override_settings(ROOT_URLCONF=URLS)
    def test_an_or_composition_still_lets_the_guest_in(self):
        """``IsAuthenticated | X`` is an OperandHolder — a name E001 reads as
        a position taken, while the left operand admits every guest."""
        assert "ComposedGateView" in _reported(check_guest_admitted_by_the_gate())

    @override_settings(ROOT_URLCONF=URLS)
    def test_the_whole_fixture_reports_exactly_the_two_ambiguous_views(self):
        assert _reported(check_guest_admitted_by_the_gate()) == [
            "BusinessGatedView",
            "ComposedGateView",
        ]


# ------------------------------------------------- a raise is not a verdict


@pytest.mark.usefixtures("axis_on")
class TestAGateThatCannotBeRun:
    @override_settings(ROOT_URLCONF=URLS)
    def test_a_raising_gate_is_skipped_in_silence(self):
        """Reporting what could not be evaluated is how a check earns its
        place in SILENCED_SYSTEM_CHECKS."""
        assert "UnprobeableView" not in _reported(check_guest_admitted_by_the_gate())

    @override_settings(ROOT_URLCONF=URLS)
    def test_the_gate_really_does_raise(self):
        """Otherwise the test above would pass for the wrong reason."""
        from tests.adoption_guest_gate_urls import UnprobeableView

        with pytest.raises(RuntimeError):
            gate_admits(UnprobeableView, GuestPrincipal())


# -------------------------------------------------------- the two principals


class TestThePrincipals:
    def test_a_guest_is_authenticated_and_anonymous_at_once(self):
        """The stapel-auth guest row's own shape — it passes IsAuthenticated
        and fails IsNotAnonymousUser, which is the whole ambiguity."""
        from rest_framework.permissions import IsAuthenticated

        from stapel_core.django.api.permissions import IsNotAnonymousUser
        from tests.adoption_guest_gate_urls import _Base

        guest = GuestPrincipal()
        assert guest.is_authenticated is True and guest.is_anonymous is True

        class _Probe(_Base):
            permission_classes = [IsAuthenticated]

        class _Identity(_Base):
            permission_classes = [IsNotAnonymousUser]

        assert gate_admits(_Probe, guest) is True
        assert gate_admits(_Identity, guest) is False

    def test_an_unauthenticated_caller_fails_is_authenticated(self):
        from django.contrib.auth.models import AnonymousUser
        from rest_framework.permissions import IsAuthenticated

        from tests.adoption_guest_gate_urls import _Base

        class _Probe(_Base):
            permission_classes = [IsAuthenticated]

        assert gate_admits(_Probe, AnonymousUser()) is False


# ---------------------------------------------- no overlap with the siblings


@pytest.mark.usefixtures("axis_on")
class TestItDoesNotRepeatTheStaticCheck:
    @override_settings(ROOT_URLCONF=STATIC_URLS)
    def test_a_bare_is_authenticated_view_is_left_to_e001(self):
        """Two findings for one view is the noise that gets a tag muted."""
        static = [
            f.msg for f in check_anonymous_stance_declared()
            if f.id == E001_ANONYMOUS_STANCE_UNDECLARED
        ]
        assert any("SilentView" in m for m in static)
        assert "SilentView" not in _reported(check_guest_admitted_by_the_gate())

    @override_settings(ROOT_URLCONF=STATIC_URLS)
    def test_a_view_on_the_drf_default_is_left_to_w001(self):
        """Blaming each view for a decision made in settings.py is the flood
        W001 exists to avoid; W003 inherits that rule."""
        assert "DefaultingView" not in _reported(check_guest_admitted_by_the_gate())


# ------------------------------------------------------ premise, registration


@override_settings(ROOT_URLCONF=URLS)
def test_axis_off_means_silence():
    """No guest sessions exist in this deployment — no premise, no finding,
    even though the URLconf is full of the shape."""
    assert check_guest_admitted_by_the_gate() == []


@pytest.mark.usefixtures("axis_on")
def test_no_root_urlconf_is_not_a_crash():
    with override_settings(ROOT_URLCONF=""):
        assert check_guest_admitted_by_the_gate() == []


@pytest.mark.usefixtures("axis_on")
@override_settings(ROOT_URLCONF=URLS)
def test_a_library_owned_view_names_its_package(monkeypatch):
    from tests import adoption_guest_gate_urls as fixture

    monkeypatch.setattr(
        fixture.BusinessGatedView, "__module__", "stapel_widgets.views"
    )
    [warning] = [
        f for f in check_guest_admitted_by_the_gate()
        if "BusinessGatedView" in f.msg
    ]
    assert "stapel_widgets" in warning.hint


def test_the_probe_is_registered_under_the_adoption_tag():
    """Without this, the check would run only where a test calls it."""
    registry = django_checks.registry.registry
    registered = {
        getattr(check, "__name__", "") for check in registry.get_checks()
    }
    assert "check_guest_admitted_by_the_gate" in registered
