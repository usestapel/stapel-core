"""Adoption checks — the anonymous-stance gate (tag ``stapel_adoption``).

The behaviour under test is deliberately asymmetric: *silence* is the error,
and every explicit answer — "guests may come in", "guests may not", "a
stronger gate decides" — is green. A version of this check that demanded one
particular answer would be wrong on the first consumer that sells guest
access, and would be muted before it ever caught anything.
"""
import pytest
from django.core import checks as django_checks
from django.test import override_settings

from stapel_core.django.adoption_checks import (
    E001_ANONYMOUS_STANCE_UNDECLARED,
    E002_BAD_ANONYMOUS_DECLARATION,
    W001_DEFAULT_GATE_IS_BARE,
    W002_LIBRARY_STANCE_UNDECLARED,
    anonymous_axis_enabled,
    check_anonymous_stance_declared,
    check_default_permission_gate,
    library_package,
)

URLS = "tests.adoption_anonymous_urls"


@pytest.fixture
def axis_on(monkeypatch):
    """Force the AUTH_ANONYMOUS premise to hold (stapel_auth is not installed
    in core's own test env — the axis lives in the auth module)."""
    monkeypatch.setattr(
        "stapel_core.django.adoption_checks.anonymous_axis_enabled", lambda: True
    )


def _ids(findings):
    return sorted(f.id for f in findings)


def _msgs(findings, check_id):
    return [f.msg for f in findings if f.id == check_id]


# --------------------------------------------------------------- the axis


class TestAxisDetection:
    def test_axis_off_without_stapel_auth(self):
        """No auth module installed — no guest sessions exist, no premise."""
        assert anonymous_axis_enabled() is False

    def test_axis_reads_settings_when_module_installed(self, monkeypatch):
        import django.apps

        monkeypatch.setattr(
            django.apps.apps, "is_installed", lambda name: name == "stapel_auth"
        )
        with override_settings(STAPEL_AUTH={"AUTH_ANONYMOUS": False}):
            assert anonymous_axis_enabled() is False
        with override_settings(STAPEL_AUTH={"AUTH_ANONYMOUS": True}):
            assert anonymous_axis_enabled() is True
        # The axis defaults to True in stapel-auth: an installed module with
        # no explicit setting still means guests exist.
        with override_settings(STAPEL_AUTH={}):
            assert anonymous_axis_enabled() is True

    @override_settings(ROOT_URLCONF=URLS)
    def test_axis_off_means_silence(self):
        """The premise is false — the check has nothing to say, even though
        the very same URLconf is full of bare-IsAuthenticated views."""
        assert check_anonymous_stance_declared() == []
        assert check_default_permission_gate() == []


# ------------------------------------------------------- E001: the silence


@pytest.mark.usefixtures("axis_on")
class TestSilenceIsTheError:
    @override_settings(ROOT_URLCONF=URLS)
    def test_bare_is_authenticated_without_a_stance_is_an_error(self):
        findings = check_anonymous_stance_declared()
        errors = _msgs(findings, E001_ANONYMOUS_STANCE_UNDECLARED)
        assert any("SilentView" in m for m in errors)
        assert all(
            isinstance(f, django_checks.Error)
            for f in findings if f.id == E001_ANONYMOUS_STANCE_UNDECLARED
        )

    @override_settings(ROOT_URLCONF=URLS)
    def test_message_names_the_view_and_its_path(self):
        errors = _msgs(check_anonymous_stance_declared(),
                       E001_ANONYMOUS_STANCE_UNDECLARED)
        [msg] = [m for m in errors
                 if "SilentView" in m and "SilentChildView" not in m]
        assert "tests.adoption_anonymous_urls.SilentView" in msg
        assert "api/silent/" in msg

    @override_settings(ROOT_URLCONF=URLS)
    def test_inherited_permission_classes_are_not_lost(self):
        """A gate declared on a project base class still governs its children."""
        errors = _msgs(check_anonymous_stance_declared(),
                       E001_ANONYMOUS_STANCE_UNDECLARED)
        [child] = [m for m in errors if "SilentChildView" in m]
        assert "inherited from" in child
        assert "SilentBase" in child

    @override_settings(ROOT_URLCONF=URLS)
    def test_one_finding_per_view_however_many_mounts(self):
        errors = _msgs(check_anonymous_stance_declared(),
                       E001_ANONYMOUS_STANCE_UNDECLARED)
        named = [m for m in errors
                 if "SilentView" in m and "SilentChildView" not in m]
        assert len(named) == 1  # mounted at /api/silent/ and /nested/api/silent/

    @override_settings(ROOT_URLCONF=URLS)
    def test_exactly_the_silent_views_are_reported(self):
        errors = _msgs(check_anonymous_stance_declared(),
                       E001_ANONYMOUS_STANCE_UNDECLARED)
        assert len(errors) == 2, errors  # SilentView + SilentChildView

    @override_settings(ROOT_URLCONF=URLS)
    def test_hint_offers_both_answers(self):
        [hint] = {
            f.hint for f in check_anonymous_stance_declared()
            if f.id == E001_ANONYMOUS_STANCE_UNDECLARED
        }
        assert "IsNotAnonymousUser" in hint
        assert "ANONYMOUS_ALLOWED" in hint


# ------------------------------------------------ the three ways to be green


@pytest.mark.usefixtures("axis_on")
class TestEveryExplicitChoiceIsGreen:
    @override_settings(ROOT_URLCONF=URLS)
    @pytest.mark.parametrize("view_name", [
        "DeniedByPermissionView",   # 1. IsNotAnonymousUser
        "CapabilityGatedView",      # 2. a stronger/other gate
        "GuestsWelcomeView",        # 3a. declared: guests welcome
        "GuestsRefusedInBodyView",  # 3b. declared: guests refused elsewhere
        "InheritedDeclarationView",  # 3c. declaration inherited from a base
        "OpenView",
        "ComposedGateView",
    ])
    def test_view_is_not_reported(self, view_name):
        errors = _msgs(check_anonymous_stance_declared(),
                       E001_ANONYMOUS_STANCE_UNDECLARED)
        assert not [m for m in errors if view_name in m]

    @override_settings(ROOT_URLCONF=URLS)
    def test_project_default_is_not_charged_to_each_view(self):
        """A view that never wrote a permission_classes line is W001's
        business (one finding, at the setting), never E001's."""
        errors = _msgs(check_anonymous_stance_declared(),
                       E001_ANONYMOUS_STANCE_UNDECLARED)
        assert not [m for m in errors if "DefaultingView" in m]


# ------------------------------------------ E002: a typo is not a declaration


@pytest.mark.usefixtures("axis_on")
class TestMalformedDeclaration:
    @override_settings(ROOT_URLCONF=URLS)
    def test_unknown_value_is_reported_not_accepted(self):
        errors = _msgs(check_anonymous_stance_declared(),
                       E002_BAD_ANONYMOUS_DECLARATION)
        assert len(errors) == 1
        assert "TypoedView" in errors[0]
        assert "'yes'" in errors[0]

    @override_settings(ROOT_URLCONF=URLS)
    def test_a_typo_does_not_also_raise_e001(self):
        errors = _msgs(check_anonymous_stance_declared(),
                       E001_ANONYMOUS_STANCE_UNDECLARED)
        assert not [m for m in errors if "TypoedView" in m]


# ------------------------------------------------- W001: the project default


@pytest.mark.usefixtures("axis_on")
class TestProjectDefault:
    def test_bare_default_is_one_warning(self, settings):
        from rest_framework.settings import api_settings

        settings.REST_FRAMEWORK = {
            "DEFAULT_PERMISSION_CLASSES": [
                "rest_framework.permissions.IsAuthenticated"
            ],
        }
        api_settings.reload()
        try:
            findings = check_default_permission_gate()
            assert _ids(findings) == [W001_DEFAULT_GATE_IS_BARE]
            assert isinstance(findings[0], django_checks.Warning)
        finally:
            api_settings.reload()

    def test_stricter_default_is_silent(self, settings):
        from rest_framework.settings import api_settings

        settings.REST_FRAMEWORK = {
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsNotAnonymousUser"
            ],
        }
        api_settings.reload()
        try:
            assert check_default_permission_gate() == []
        finally:
            api_settings.reload()


# ------------------------------------------- level follows who can act (W002)


@pytest.mark.usefixtures("axis_on")
class TestLibraryOwnedViews:
    """A silence inside an installed ``stapel_*`` wheel is the same finding,
    but the reader cannot edit that file — blocking their deploy over it is
    how the whole tag ends up in SILENCED_SYSTEM_CHECKS."""

    @override_settings(ROOT_URLCONF=URLS)
    def test_library_view_is_a_warning_naming_its_package(self, monkeypatch):
        from tests import adoption_anonymous_urls as fixture

        monkeypatch.setattr(fixture.SilentView, "__module__", "stapel_widgets.views")
        findings = check_anonymous_stance_declared()
        [warning] = [f for f in findings
                     if f.id == W002_LIBRARY_STANCE_UNDECLARED]
        assert isinstance(warning, django_checks.Warning)
        assert "stapel_widgets" in warning.hint
        assert not [m for m in _msgs(findings, E001_ANONYMOUS_STANCE_UNDECLARED)
                    if "SilentView" in m and "SilentChildView" not in m]

    def test_ownership_is_decided_by_import_path_not_app_marker(self):
        from tests import adoption_anonymous_urls as fixture

        assert library_package(fixture.SilentView) is None

        class FromAWheel:
            __module__ = "stapel_profiles.views"

        class LocalStapelStyleApp:
            __module__ = "apps.tools.views"

        assert library_package(FromAWheel) == "stapel_profiles"
        # A project's own app that opted into the module protocol is still
        # the project's source, so it stays an Error.
        assert library_package(LocalStapelStyleApp) is None


# ------------------------------------------------------------ registration


def test_checks_are_registered_under_the_adoption_tag():
    registry = django_checks.registry.registry
    tags = {tag for check in registry.get_checks()
            for tag in getattr(check, "tags", ())}
    assert "stapel_adoption" in tags


def test_no_root_urlconf_is_not_a_crash(monkeypatch):
    """A standalone package harness has no URLs to survey."""
    monkeypatch.setattr(
        "stapel_core.django.adoption_checks.anonymous_axis_enabled", lambda: True
    )
    with override_settings(ROOT_URLCONF=""):
        assert check_anonymous_stance_declared() == []
