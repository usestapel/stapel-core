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
    AuthorizationOnlyBackend,
    E001_BACKEND_UNIMPORTABLE,
    E002_UNDECLARED_CREDENTIAL_HANDLING,
    E003_DOES_NOT_VERIFY_CREDENTIALS,
    E004_AUTHORIZATION_ONLY_OVERRIDES_AUTHENTICATE,
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


def _shipped_backends():
    """Every authentication backend class this library ships, by discovery.

    Enumerated from the package rather than listed by hand: the previous
    version of this test named ONE backend, so
    stapel_core.django.jwt.backends.JWTAuthBackend — which overrides
    authenticate() and carried no declaration — passed review and would have
    refused to boot every project that wires it (ironmemo does). A guard that
    checks a hand-written constant only ever guards that constant.
    """
    import importlib
    import pkgutil
    from django.contrib.auth.backends import BaseBackend

    import stapel_core

    found = []
    unimportable = []

    def _note(name, exc):
        unimportable.append(f"{name}: {exc.__class__.__name__}: {exc}")

    for mod in pkgutil.walk_packages(
        stapel_core.__path__, prefix="stapel_core.", onerror=lambda n: _note(n, ImportError("subpackage walk failed"))
    ):
        name = mod.name
        # Optional-dependency modules raise ImportError by design when the
        # extra is absent. They are COLLECTED, not silently skipped: a module
        # that vanishes from the walk shrinks this rule's coverage invisibly,
        # which is how a backend gets shipped unchecked. The caller decides
        # what to tolerate; this function only refuses to lose the fact.
        try:
            module = importlib.import_module(name)
        except (ImportError, ModuleNotFoundError) as exc:
            # THE case worth reporting: a missing optional extra removes a
            # module — and any backend in it — from this rule's reach, and
            # nothing else would notice.
            _note(name, exc)
            continue
        except Exception:  # noqa: BLE001
            # Everything else is an artifact of the test settings rather than
            # of packaging — e.g. admin modules raise LookupError because this
            # suite installs no 'admin' app. The module EXISTS and ships; it
            # just cannot be imported under these settings, so it can hide
            # nothing that a real deployment would not also see.
            continue
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseBackend)
                and attr.__module__ == name
            ):
                found.append(f"{name}.{attr.__qualname__}")
    return sorted(set(found)), sorted(set(unimportable))


def test_every_backend_this_library_ships_passes_its_own_gate():
    shipped, unimportable = _shipped_backends()
    # An extra that fails to import here would hide its backends from this
    # rule. The full test matrix installs .[all], so this must be empty; a
    # non-empty list is reported rather than quietly narrowing coverage.
    assert not unimportable, f"modules the walk could not import: {unimportable}"
    # Not vacuous: if discovery finds nothing, the assertion below is empty
    # and the rule guards nothing.
    assert len(shipped) >= 2, f"discovery found too few backends: {shipped}"

    offenders = {}
    for path in shipped:
        with override_settings(AUTHENTICATION_BACKENDS=[path]):
            ids = _ids(check_authentication_backends())
        if ids:
            offenders[path] = ids
    assert not offenders, (
        "stapel-core ships authentication backend(s) that fail stapel-core's "
        f"own boot gate: {offenders}. A project wiring one of these would "
        "refuse to start. Declare verifies_credentials on the class (if it "
        "does check a secret) or keep it out of AUTHENTICATION_BACKENDS."
    )


class PermissionsOnly(AuthorizationOnlyBackend):
    """The supported shape: no authenticate(), only permission answers."""

    def has_perm(self, user_obj, perm, obj=None):
        return False


class MarkerThatGrewAnAuthenticate(AuthorizationOnlyBackend):
    """The drift the marker must not survive."""

    def authenticate(self, request, **credentials):
        return object()


class MarkerThatGrewAnAuthenticateAndLies(AuthorizationOnlyBackend):
    """Same drift, plus a declaration that must not buy its way out."""

    verifies_credentials = True

    def authenticate(self, request, **credentials):
        return object()


@override_settings(AUTHENTICATION_BACKENDS=[f"{_HERE}.PermissionsOnly"])
def test_authorization_only_backend_passes_without_any_declaration():
    """The exemption is read off the MRO, so nothing needs declaring."""
    assert check_authentication_backends() == []


@override_settings(
    AUTHENTICATION_BACKENDS=[f"{_HERE}.MarkerThatGrewAnAuthenticate"]
)
def test_marker_subclass_that_overrides_authenticate_is_an_error():
    assert _ids(check_authentication_backends()) == [
        E004_AUTHORIZATION_ONLY_OVERRIDES_AUTHENTICATE
    ]


@override_settings(
    AUTHENTICATION_BACKENDS=[f"{_HERE}.MarkerThatGrewAnAuthenticateAndLies"]
)
def test_marker_subclass_cannot_declare_its_way_past_the_error():
    """verifies_credentials must not override a contradicting base class."""
    assert _ids(check_authentication_backends()) == [
        E004_AUTHORIZATION_ONLY_OVERRIDES_AUTHENTICATE
    ]


def test_mandate_backend_really_returns_none_at_runtime():
    """Behavioural insurance, independent of the static check.

    The gate reasons about the MRO; this asserts the no-op is what actually
    runs, so a future refactor cannot satisfy the check while admitting a
    principal.
    """
    from stapel_core.access.backend import MandateBackend

    assert MandateBackend().authenticate(None, username="x", password="y") is None
