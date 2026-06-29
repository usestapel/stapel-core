import pytest
from unittest.mock import MagicMock
from rest_framework.test import APIRequestFactory

from stapel_core.django.api.permissions import (
    IsStaffUser,
    IsSuperUser,
    ReadOnlyOrSuperUser,
    ReadOnlyOrStaff,
    IsServiceRequest,
    IsNotAnonymousUser,
)

_factory = APIRequestFactory()
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _user(is_authenticated=True, is_staff=False, is_superuser=False, is_anonymous=False):
    user = MagicMock()
    user.is_authenticated = is_authenticated
    user.is_staff = is_staff
    user.is_superuser = is_superuser
    user.is_anonymous = is_anonymous
    return user


def _request(method="GET", user=None, is_service=False):
    req = getattr(_factory, method.lower())("/")
    req.user = user if user is not None else _user(is_authenticated=False)
    req.is_service_request = is_service
    return req


# ---------------------------------------------------------------------------
# IsStaffUser
# ---------------------------------------------------------------------------

class TestIsStaffUser:
    perm = IsStaffUser()

    def test_staff_allowed(self):
        req = _request(user=_user(is_staff=True))
        assert self.perm.has_permission(req, None)

    def test_superuser_allowed(self):
        req = _request(user=_user(is_superuser=True))
        assert self.perm.has_permission(req, None)

    def test_staff_and_superuser_allowed(self):
        req = _request(user=_user(is_staff=True, is_superuser=True))
        assert self.perm.has_permission(req, None)

    def test_regular_authenticated_denied(self):
        req = _request(user=_user())
        assert not self.perm.has_permission(req, None)

    def test_anonymous_denied(self):
        req = _request(user=_user(is_authenticated=False))
        assert not self.perm.has_permission(req, None)


# ---------------------------------------------------------------------------
# IsSuperUser
# ---------------------------------------------------------------------------

class TestIsSuperUser:
    perm = IsSuperUser()

    def test_superuser_allowed(self):
        req = _request(user=_user(is_superuser=True))
        assert self.perm.has_permission(req, None)

    def test_staff_only_denied(self):
        req = _request(user=_user(is_staff=True))
        assert not self.perm.has_permission(req, None)

    def test_regular_user_denied(self):
        req = _request(user=_user())
        assert not self.perm.has_permission(req, None)

    def test_anonymous_denied(self):
        req = _request(user=_user(is_authenticated=False))
        assert not self.perm.has_permission(req, None)


# ---------------------------------------------------------------------------
# ReadOnlyOrSuperUser
# ---------------------------------------------------------------------------

class TestReadOnlyOrSuperUser:
    perm = ReadOnlyOrSuperUser()

    @pytest.mark.parametrize("method", SAFE_METHODS)
    def test_safe_methods_allowed_for_anon(self, method):
        req = _request(method=method, user=_user(is_authenticated=False))
        assert self.perm.has_permission(req, None)

    @pytest.mark.parametrize("method", SAFE_METHODS)
    def test_safe_methods_allowed_for_regular_user(self, method):
        req = _request(method=method, user=_user())
        assert self.perm.has_permission(req, None)

    def test_post_superuser_allowed(self):
        req = _request("POST", user=_user(is_superuser=True))
        assert self.perm.has_permission(req, None)

    def test_post_regular_user_denied(self):
        req = _request("POST", user=_user())
        assert not self.perm.has_permission(req, None)

    def test_post_staff_only_denied(self):
        req = _request("POST", user=_user(is_staff=True))
        assert not self.perm.has_permission(req, None)

    def test_post_anon_denied(self):
        req = _request("POST", user=_user(is_authenticated=False))
        assert not self.perm.has_permission(req, None)

    @pytest.mark.parametrize("method", UNSAFE_METHODS)
    def test_unsafe_methods_require_superuser(self, method):
        req = _request(method=method, user=_user())
        assert not self.perm.has_permission(req, None)
        req2 = _request(method=method, user=_user(is_superuser=True))
        assert self.perm.has_permission(req2, None)


# ---------------------------------------------------------------------------
# ReadOnlyOrStaff
# ---------------------------------------------------------------------------

class TestReadOnlyOrStaff:
    perm = ReadOnlyOrStaff()

    @pytest.mark.parametrize("method", SAFE_METHODS)
    def test_safe_methods_allowed_for_anon(self, method):
        req = _request(method=method, user=_user(is_authenticated=False))
        assert self.perm.has_permission(req, None)

    def test_post_staff_allowed(self):
        req = _request("POST", user=_user(is_staff=True))
        assert self.perm.has_permission(req, None)

    def test_post_superuser_allowed(self):
        req = _request("POST", user=_user(is_superuser=True))
        assert self.perm.has_permission(req, None)

    def test_post_regular_denied(self):
        req = _request("POST", user=_user())
        assert not self.perm.has_permission(req, None)

    def test_post_anon_denied(self):
        req = _request("POST", user=_user(is_authenticated=False))
        assert not self.perm.has_permission(req, None)


# ---------------------------------------------------------------------------
# IsServiceRequest
# ---------------------------------------------------------------------------

class TestIsServiceRequest:
    perm = IsServiceRequest()

    def test_service_request_allowed(self):
        req = _request(is_service=True)
        assert self.perm.has_permission(req, None)

    def test_non_service_denied(self):
        req = _request(is_service=False)
        assert not self.perm.has_permission(req, None)

    def test_missing_attribute_defaults_to_denied(self):
        req = _factory.get("/")
        req.user = _user()
        # No is_service_request attribute set
        assert not self.perm.has_permission(req, None)

    def test_staff_without_service_flag_denied(self):
        req = _request(user=_user(is_staff=True), is_service=False)
        assert not self.perm.has_permission(req, None)


# ---------------------------------------------------------------------------
# IsNotAnonymousUser
# ---------------------------------------------------------------------------

class TestIsNotAnonymousUser:
    perm = IsNotAnonymousUser()

    def test_regular_authenticated_allowed(self):
        req = _request(user=_user(is_anonymous=False))
        assert self.perm.has_permission(req, None)

    def test_authenticated_with_anonymous_flag_denied(self):
        req = _request(user=_user(is_authenticated=True, is_anonymous=True))
        assert not self.perm.has_permission(req, None)

    def test_unauthenticated_denied(self):
        req = _request(user=_user(is_authenticated=False))
        assert not self.perm.has_permission(req, None)

    def test_staff_without_anonymous_flag_allowed(self):
        req = _request(user=_user(is_staff=True, is_anonymous=False))
        assert self.perm.has_permission(req, None)
