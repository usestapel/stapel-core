"""AUTHENTICATION_BACKENDS boot gate (stapel_core.django.auth_backend_checks).

The class of defect being closed: a library ships an authentication backend
that resolves a principal without checking a secret, a product lists its
dotted path in AUTHENTICATION_BACKENDS, and every caller of
django.contrib.auth.authenticate() in that process silently accepts any
password. Nobody reviewing either repository alone sees it. The gate makes
the seam itself refuse to boot.
"""
from django.contrib.auth.backends import BaseBackend, ModelBackend
from django.test import override_settings

from stapel_core.django.auth_backend_checks import (
    E001_BACKEND_UNIMPORTABLE,
    E002_UNDECLARED_CREDENTIAL_HANDLING,
    E003_DOES_NOT_VERIFY_CREDENTIALS,
    check_authentication_backends,
)

_HERE = "tests.test_auth_backend_checks"


class SilentBypassBackend(BaseBackend):
    """The audited shape: a user comes back, no secret was ever compared."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        return None


class DeclaredSafeBackend(ModelBackend):
    verifies_credentials = True

    def authenticate(self, request, username=None, password=None, **kwargs):
        return None


class DeclaredUnsafeBackend(BaseBackend):
    verifies_credentials = False

    def authenticate(self, request, username=None, password=None, **kwargs):
        return None


class InheritsDjangoPasswordCheck(ModelBackend):
    """Narrows the lookup only; Django's password check is still in force."""


def _ids(errors):
    return sorted(e.id for e in errors)


@override_settings(AUTHENTICATION_BACKENDS=[f"{_HERE}.SilentBypassBackend"])
def test_undeclared_override_is_an_error():
    assert _ids(check_authentication_backends()) == [
        E002_UNDECLARED_CREDENTIAL_HANDLING
    ]


@override_settings(AUTHENTICATION_BACKENDS=[f"{_HERE}.DeclaredUnsafeBackend"])
def test_explicitly_credential_less_backend_is_an_error():
    assert _ids(check_authentication_backends()) == [E003_DOES_NOT_VERIFY_CREDENTIALS]


@override_settings(AUTHENTICATION_BACKENDS=[f"{_HERE}.DeclaredSafeBackend"])
def test_declared_backend_passes():
    assert check_authentication_backends() == []


@override_settings(
    AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"]
)
def test_django_stock_backend_passes():
    assert check_authentication_backends() == []


@override_settings(AUTHENTICATION_BACKENDS=[f"{_HERE}.InheritsDjangoPasswordCheck"])
def test_subclass_that_does_not_override_authenticate_passes():
    assert check_authentication_backends() == []


@override_settings(AUTHENTICATION_BACKENDS=["nope.NotAThing"])
def test_unimportable_backend_is_an_error():
    assert _ids(check_authentication_backends()) == [E001_BACKEND_UNIMPORTABLE]


@override_settings(
    AUTHENTICATION_BACKENDS=[f"{_HERE}.SilentBypassBackend"],
    STAPEL_SECURITY={"REVIEWED_AUTH_BACKENDS": [f"{_HERE}.SilentBypassBackend"]},
)
def test_reviewed_backend_is_exempt():
    """The escape hatch for third-party backends: explicit, per-project."""
    assert check_authentication_backends() == []


@override_settings(
    AUTHENTICATION_BACKENDS=["stapel_core.django.jwt.session.EmailAuthBackend"]
)
def test_the_backend_this_library_ships_passes_its_own_gate():
    assert check_authentication_backends() == []
