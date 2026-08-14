"""The CORS pair: allow-all and credentials may not be armed together.

Audit CDN-01 was reported against an nginx vhost. The same defect lived a
second time in this library's shared settings, where it applied to every
service: credentials were asserted unconditionally while allow-all was an
environment toggle. django-cors-headers, handed both, reflects the caller's
Origin — so the vhost fix alone would have left the wider instance standing.
"""
from django.test import override_settings

from stapel_core.security.cors import derive_allow_credentials
from stapel_core.django.cors_checks import (
    E001_CREDENTIALS_WITH_ALL_ORIGINS,
    W002_CREDENTIALS_WITHOUT_ALLOWLIST,
    check_cors_credentials,
)


def _ids(errors):
    return [e.id for e in errors]


@override_settings(
    CORS_ALLOW_ALL_ORIGINS=True,
    CORS_ALLOW_CREDENTIALS=True,
    CORS_ALLOWED_ORIGINS=[],
)
def test_allow_all_with_credentials_is_refused():
    assert _ids(check_cors_credentials()) == [E001_CREDENTIALS_WITH_ALL_ORIGINS]


@override_settings(
    CORS_ALLOW_ALL_ORIGINS=True,
    CORS_ALLOW_CREDENTIALS=False,
    CORS_ALLOWED_ORIGINS=[],
)
def test_allow_all_without_credentials_is_fine():
    """The local-development shape the toggle was actually documented for."""
    assert check_cors_credentials() == []


@override_settings(
    CORS_ALLOW_ALL_ORIGINS=False,
    CORS_ALLOW_CREDENTIALS=True,
    CORS_ALLOWED_ORIGINS=["https://app.example.com"],
)
def test_named_origins_with_credentials_is_the_supported_shape():
    assert check_cors_credentials() == []


@override_settings(
    CORS_ALLOW_ALL_ORIGINS=False,
    CORS_ALLOW_CREDENTIALS=True,
    CORS_ALLOWED_ORIGINS=[],
    CORS_ALLOWED_ORIGIN_REGEXES=[],
)
def test_credentials_with_nothing_allowed_is_a_warning():
    """Not dangerous, just inert — a setting stating an intent nothing reads."""
    assert _ids(check_cors_credentials()) == [W002_CREDENTIALS_WITHOUT_ALLOWLIST]


def test_dev_toggle_alone_cannot_arm_credentials():
    """The one-env-var reproduction of CDN-01, closed at the source."""
    assert derive_allow_credentials(True, []) is False


def test_allow_all_beats_a_named_allowlist():
    """Allow-all is the dangerous half; naming origins must not re-arm it."""
    assert derive_allow_credentials(True, ["https://app.example.com"]) is False


def test_named_origins_alone_do_arm_credentials():
    """Not vacuous — the supported shape still gets credentials."""
    assert derive_allow_credentials(False, ["https://app.example.com"]) is True


def test_no_origins_no_credentials():
    assert derive_allow_credentials(False, []) is False


def test_settings_module_uses_the_shared_rule():
    """The settings module must not re-spell the rule inline.

    Importing stapel_core.django.settings directly is impossible (the package
    pulls in DRF, which demands configured settings), so this reads the
    source: it is the only way to prove the two places did not drift.
    """
    from pathlib import Path

    import stapel_core

    src = (Path(stapel_core.__file__).parent / "django" / "settings.py").read_text()
    assert "derive_allow_credentials(" in src, (
        "django/settings.py no longer calls the shared rule — a second, "
        "inline spelling is exactly how CDN-01 survived in two places."
    )
    assert "CORS_ALLOW_CREDENTIALS = True" not in src


def test_the_check_is_registered_not_merely_importable():
    """The recurring defect here is a mechanism nobody wires up.

    Calling the function directly proves the logic; it does not prove Django
    will ever run it. This asserts the tag is in the registry, which is what
    a deployment actually depends on.
    """
    from django.core.checks import registry

    tags = registry.registry.get_checks(include_deployment_checks=False)
    assert any(
        getattr(c, "__name__", "") == "check_cors_credentials" for c in tags
    ), "stapel_cors check is not registered — apps.ready() does not import it"
