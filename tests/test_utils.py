import uuid
import pytest
from unittest.mock import MagicMock
from django.test import RequestFactory, override_settings
from django.http import HttpResponse

from stapel_core.django.jwt.utils import (
    extract_jwt_from_request,
    set_jwt_cookies,
    serialize_user_to_jwt_data,
    get_or_create_user_from_jwt,
    load_jwt_config_from_settings,
    setup_centralized_admin_logout,
)

_factory = RequestFactory()


# ---------------------------------------------------------------------------
# extract_jwt_from_request
# ---------------------------------------------------------------------------

class TestExtractJwtFromRequest:
    def _req(self, cookies=None, auth_header=None):
        req = _factory.get("/")
        req.COOKIES = cookies or {}
        if auth_header:
            req.META["HTTP_AUTHORIZATION"] = auth_header
        return req

    def test_access_from_cookie(self):
        req = self._req(cookies={"stapel_jwt": "tok.abc"})
        access, refresh = extract_jwt_from_request(req)
        assert access == "tok.abc"
        assert refresh is None

    def test_refresh_from_cookie(self):
        req = self._req(cookies={"stapel_refresh_jwt": "ref.tok"})
        access, refresh = extract_jwt_from_request(req)
        assert refresh == "ref.tok"
        assert access is None

    def test_both_from_cookies(self):
        req = self._req(cookies={"stapel_jwt": "acc", "stapel_refresh_jwt": "ref"})
        access, refresh = extract_jwt_from_request(req)
        assert access == "acc"
        assert refresh == "ref"

    def test_access_from_authorization_header(self):
        req = self._req(auth_header="Bearer header.token.here")
        access, refresh = extract_jwt_from_request(req)
        assert access == "header.token.here"

    def test_cookie_takes_precedence_over_header(self):
        req = self._req(
            cookies={"stapel_jwt": "cookie-tok"},
            auth_header="Bearer header-tok",
        )
        access, _ = extract_jwt_from_request(req)
        assert access == "cookie-tok"

    def test_non_bearer_header_ignored(self):
        req = self._req(auth_header="Basic dXNlcjpwYXNz")
        access, _ = extract_jwt_from_request(req)
        assert access is None

    def test_no_tokens(self):
        req = self._req()
        access, refresh = extract_jwt_from_request(req)
        assert access is None
        assert refresh is None

    @override_settings(JWT_COOKIE_NAME="custom_jwt", JWT_REFRESH_COOKIE_NAME="custom_ref")
    def test_custom_cookie_names(self):
        req = self._req(cookies={"custom_jwt": "a", "custom_ref": "r"})
        access, refresh = extract_jwt_from_request(req)
        assert access == "a"
        assert refresh == "r"


# ---------------------------------------------------------------------------
# set_jwt_cookies
# ---------------------------------------------------------------------------

class TestSetJwtCookies:
    def test_access_cookie_set(self):
        response = HttpResponse()
        set_jwt_cookies(response, "access-tok")
        assert "stapel_jwt" in response.cookies
        assert response.cookies["stapel_jwt"].value == "access-tok"

    def test_refresh_cookie_not_set_when_none(self):
        response = HttpResponse()
        set_jwt_cookies(response, "access-tok", refresh_token=None)
        assert "stapel_refresh_jwt" not in response.cookies

    def test_refresh_cookie_set_when_provided(self):
        response = HttpResponse()
        set_jwt_cookies(response, "access-tok", refresh_token="refresh-tok")
        assert "stapel_refresh_jwt" in response.cookies
        assert response.cookies["stapel_refresh_jwt"].value == "refresh-tok"

    @override_settings(JWT_COOKIE_NAME="my_jwt", JWT_COOKIE_HTTPONLY=True, JWT_COOKIE_SECURE=True)
    def test_custom_settings_applied(self):
        response = HttpResponse()
        set_jwt_cookies(response, "tok")
        cookie = response.cookies["my_jwt"]
        assert cookie["httponly"]
        assert cookie["secure"]

    @override_settings(JWT_COOKIE_SAMESITE="Strict")
    def test_samesite_setting(self):
        response = HttpResponse()
        set_jwt_cookies(response, "tok")
        assert response.cookies["stapel_jwt"]["samesite"] == "Strict"


# ---------------------------------------------------------------------------
# serialize_user_to_jwt_data
# ---------------------------------------------------------------------------

class TestSerializeUserToJwtData:
    def _user(self, **kwargs):
        u = MagicMock()
        u.pk = str(uuid.uuid4())
        u.email = "u@example.com"
        u.username = "alice"
        u.is_staff = False
        u.is_superuser = False
        u.is_active = True
        u.is_anonymous = False
        u.auth_type = "email"
        u.phone = None
        for k, v in kwargs.items():
            setattr(u, k, v)
        return u

    def test_basic_fields(self):
        user = self._user()
        data = serialize_user_to_jwt_data(user)
        assert data["user_id"] == user.pk
        assert data["email"] == "u@example.com"
        assert data["username"] == "alice"
        assert data["is_staff"] is False
        assert data["is_superuser"] is False
        assert data["is_active"] is True

    def test_optional_phone_included_when_set(self):
        user = self._user(phone="+79001234567")
        data = serialize_user_to_jwt_data(user)
        assert data["phone"] == "+79001234567"

    def test_phone_not_included_when_none(self):
        user = self._user(phone=None)
        data = serialize_user_to_jwt_data(user)
        assert "phone" not in data

    def test_is_anonymous_included(self):
        user = self._user(is_anonymous=True)
        data = serialize_user_to_jwt_data(user)
        assert data["is_anonymous"] is True

    def test_auth_type_included(self):
        user = self._user(auth_type="oauth")
        data = serialize_user_to_jwt_data(user)
        assert data["auth_type"] == "oauth"


# ---------------------------------------------------------------------------
# load_jwt_config_from_settings
# ---------------------------------------------------------------------------

class TestLoadJwtConfigFromSettings:
    @override_settings(
        JWT_SECRET_KEY="my-secret-key-at-least-32-bytes-long",
        JWT_ALGORITHM="HS256",
        JWT_ISSUER="test-issuer",
        JWT_AUDIENCE=None,
        JWT_COOKIE_NAME="stapel_jwt",
        JWT_REFRESH_COOKIE_NAME="stapel_refresh_jwt",
    )
    def test_hs256_config(self):
        cfg = load_jwt_config_from_settings()
        assert cfg.algorithm == "HS256"
        assert cfg.secret_key == "my-secret-key-at-least-32-bytes-long"
        assert cfg.issuer == "test-issuer"

    @override_settings(
        JWT_ALGORITHM="HS256",
        JWT_ISSUER="stapel-auth",
        JWT_JWKS_URL=None,
        JWT_AUDIENCE=None,
        JWT_COOKIE_NAME="stapel_jwt",
        JWT_REFRESH_COOKIE_NAME="stapel_refresh_jwt",
    )
    def test_jwks_url_derived_from_http_issuer(self):
        with override_settings(
            JWT_ISSUER="https://auth.example.com",
            JWT_SECRET_KEY="secret-key-for-testing-32bytes-long",
        ):
            cfg = load_jwt_config_from_settings()
            assert cfg.jwks_url == "https://auth.example.com/auth/.well-known/jwks.json"

    @override_settings(
        JWT_ALGORITHM="HS256",
        JWT_ISSUER="non-http-issuer",
        JWT_JWKS_URL=None,
        JWT_AUDIENCE=None,
        JWT_COOKIE_NAME="stapel_jwt",
        JWT_REFRESH_COOKIE_NAME="stapel_refresh_jwt",
        JWT_SECRET_KEY="secret-key-for-testing-32bytes-long",
    )
    def test_no_jwks_url_for_non_http_issuer(self):
        cfg = load_jwt_config_from_settings()
        assert cfg.jwks_url is None

    @override_settings(
        JWT_COOKIE_SECURE=True,
        JWT_COOKIE_SAMESITE="Strict",
        JWT_COOKIE_DOMAIN=".example.com",
        JWT_ALGORITHM="HS256",
        JWT_ISSUER="iss",
        JWT_AUDIENCE=None,
        JWT_COOKIE_NAME="stapel_jwt",
        JWT_REFRESH_COOKIE_NAME="stapel_refresh_jwt",
        JWT_SECRET_KEY="secret-key-for-testing-32bytes-long",
    )
    def test_cookie_settings_passed_through(self):
        cfg = load_jwt_config_from_settings()
        assert cfg.cookie_secure is True
        assert cfg.cookie_samesite == "Strict"
        assert cfg.cookie_domain == ".example.com"


# ---------------------------------------------------------------------------
# get_or_create_user_from_jwt
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestGetOrCreateUserFromJwt:
    def _data(self, **kwargs):
        uid = str(uuid.uuid4())
        data = {
            "user_id": uid,
            "email": f"user_{uid[:8]}@example.com",
            "username": f"user_{uid[:8]}",
            "is_staff": False,
            "is_superuser": False,
            "is_active": True,
        }
        data.update(kwargs)
        return data

    def test_creates_new_user(self):
        data = self._data()
        user = get_or_create_user_from_jwt(data)
        assert user is not None
        assert str(user.pk) == data["user_id"]
        assert user.email == data["email"]

    def test_returns_existing_user(self):
        data = self._data()
        u1 = get_or_create_user_from_jwt(data)
        u2 = get_or_create_user_from_jwt(data)
        assert str(u1.pk) == str(u2.pk)

    def test_upgrades_is_staff(self):
        data = self._data(is_staff=False)
        user = get_or_create_user_from_jwt(data)
        assert not user.is_staff

        upgraded = self._data(user_id=str(user.pk), is_staff=True, email=data["email"], username=data["username"])
        user = get_or_create_user_from_jwt(upgraded)
        assert user.is_staff

    def test_does_not_downgrade_is_staff(self):
        data = self._data(is_staff=True)
        user = get_or_create_user_from_jwt(data)
        assert user.is_staff

        downgrade = self._data(user_id=str(user.pk), is_staff=False, email=data["email"], username=data["username"])
        user = get_or_create_user_from_jwt(downgrade)
        assert user.is_staff  # must not downgrade

    def test_upgrades_is_superuser(self):
        data = self._data(is_superuser=False)
        user = get_or_create_user_from_jwt(data)
        assert not user.is_superuser

        upgraded = self._data(user_id=str(user.pk), is_superuser=True, email=data["email"], username=data["username"])
        user = get_or_create_user_from_jwt(upgraded)
        assert user.is_superuser

    def test_does_not_downgrade_is_superuser(self):
        data = self._data(is_superuser=True)
        user = get_or_create_user_from_jwt(data)
        assert user.is_superuser

        downgrade = self._data(user_id=str(user.pk), is_superuser=False, email=data["email"], username=data["username"])
        user = get_or_create_user_from_jwt(downgrade)
        assert user.is_superuser

    def test_updates_is_active(self):
        data = self._data(is_active=True)
        user = get_or_create_user_from_jwt(data)
        assert user.is_active

        deactivated = self._data(user_id=str(user.pk), is_active=False, email=data["email"], username=data["username"])
        user = get_or_create_user_from_jwt(deactivated)
        assert not user.is_active

    @override_settings(JWT_CREATE_USERS_FROM_TOKEN=False)
    def test_returns_none_for_unknown_user_when_creation_disabled(self):
        data = self._data()
        user = get_or_create_user_from_jwt(data)
        assert user is None

    def test_returns_none_when_no_user_id(self):
        user = get_or_create_user_from_jwt({"email": "x@example.com"})
        assert user is None

    def test_created_user_has_unusable_password(self):
        data = self._data()
        user = get_or_create_user_from_jwt(data)
        assert not user.has_usable_password()

    def test_syncs_auth_type(self):
        data = self._data(auth_type="oauth")
        user = get_or_create_user_from_jwt(data)
        assert user.auth_type == "oauth"

    def test_syncs_is_anonymous(self):
        data = self._data(is_anonymous=True)
        user = get_or_create_user_from_jwt(data)
        assert user.is_anonymous is True


# ---------------------------------------------------------------------------
# setup_centralized_admin_logout (deprecated)
# ---------------------------------------------------------------------------

class TestSetupCentralizedAdminLogout:
    def test_emits_deprecation_warning(self):
        admin_site = MagicMock()
        with pytest.warns(DeprecationWarning, match="deprecated"):
            setup_centralized_admin_logout(admin_site)
