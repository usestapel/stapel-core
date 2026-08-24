"""A correct password is proof of identity, not a grant of admin access.

``JWTCookieLoginView`` is the admin login view — its template is
``admin/login.html`` — but it never named an ``authentication_form``, so
Django fell back to the plain ``AuthenticationForm``, which checks
``is_active`` and nothing else. ``form_valid()`` then logged the user in and
minted the fleet-wide JWT pair with no staff check of any kind. (The three
``is_staff`` reads in that file all sat in ``dispatch()``'s
already-authenticated branch — the credential-processing path had none.)

At a consumer that meant: any active account's username and password minted a
full JWT access/refresh pair, bypassing the deployment's password-login gate,
its lockout service (so credential stuffing ran unthrottled), its TOTP
step-up, and its tracked-session creation — the resulting session had no
tracked row, was invisible to session listings, and survived both "log out
everywhere" and password-change revocation.

Second defect, same class: ``JWTRefreshView`` called
``refresh_access_token()`` WITHOUT the ``load_user_by_uid`` loader that the
middleware passes on both of its refresh paths, so it re-minted from the
refresh token's own claims. A staff flag revoked in the database resurrected
on the next refresh, for up to ``JWT_REFRESH_TOKEN_LIFETIME``.

Every test below fails on 0.37.0.
"""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from stapel_core.django.jwt.login_views import JWTCookieLoginView
from stapel_core.django.jwt.views import JWTRefreshView

factory = RequestFactory()

LOGIN_PROVIDER = "stapel_core.django.jwt.login_views.jwt_provider"
VIEWS_PROVIDER = "stapel_core.django.jwt.views.jwt_provider"

PASSWORD = "correct-horse-battery-staple"


def _request(method="post", path="/auth/admin/login/", cookies=None):
    req = getattr(factory, method)(path, {})
    req.COOKIES = cookies or {}
    req.session = MagicMock()
    return req


def _view_for(user):
    """The view plus a form that has already validated ``user``'s password."""
    view = JWTCookieLoginView()
    view.request = _request()
    form = MagicMock()
    form.get_user.return_value = user
    return view, form


def _principal(*, is_staff=False, is_superuser=False):
    return SimpleNamespace(
        pk=uuid.uuid4(),
        is_staff=is_staff,
        is_superuser=is_superuser,
        is_active=True,
    )


# ---------------------------------------------------------------------------
# Gate 1 — the form. A non-staff credential must not reach form_valid() at all.
# ---------------------------------------------------------------------------

class TestAdminAuthenticationFormIsWired:
    def test_form_class_is_djangos_admin_form(self):
        from django.contrib.admin.forms import AdminAuthenticationForm

        assert JWTCookieLoginView().get_form_class() is AdminAuthenticationForm

    def test_subclass_may_still_name_its_own_form(self):
        class WithCaptcha(JWTCookieLoginView):
            authentication_form = AuthenticationForm

        assert WithCaptcha().get_form_class() is AuthenticationForm


@pytest.mark.django_db
class TestFormRefusesNonStaffCredentials:
    """End to end through the real form, real user, real password hash."""

    def _user(self, username, **flags):
        user = get_user_model()(username=username, **flags)
        user.set_password(PASSWORD)
        user.save()
        return user

    def _bound_form(self, username):
        form_class = JWTCookieLoginView().get_form_class()
        return form_class(
            _request(), data={"username": username, "password": PASSWORD}
        )

    def test_active_non_staff_password_does_not_validate(self):
        self._user("gate-plain")
        assert self._bound_form("gate-plain").is_valid() is False

    def test_staff_password_still_validates(self):
        self._user("gate-staff", is_staff=True)
        assert self._bound_form("gate-staff").is_valid() is True


# ---------------------------------------------------------------------------
# Gate 2 — form_valid(). Belt and braces: a subclass that swaps the form back
# to the permissive one must not reopen a full authentication bypass.
# ---------------------------------------------------------------------------

class TestFormValidRefusesNonStaff:
    def _run(self, user):
        view, form = _view_for(user)
        marker = HttpResponse("login-form-again")
        with (
            patch("stapel_core.django.jwt.login_views.login") as mock_login,
            patch(LOGIN_PROVIDER) as provider,
            patch.object(JWTCookieLoginView, "form_invalid", return_value=marker),
            patch.object(LoginView, "form_valid", return_value=HttpResponse()),
        ):
            provider.create_tokens.return_value = ("acc.tok", "ref.tok")
            resp = view.form_valid(form)
        return resp, mock_login, provider

    def test_non_staff_gets_no_tokens(self):
        _, _, provider = self._run(_principal())
        provider.create_tokens.assert_not_called()

    def test_non_staff_gets_no_session(self):
        _, mock_login, _ = self._run(_principal())
        mock_login.assert_not_called()

    def test_non_staff_gets_no_cookies(self):
        resp, _, _ = self._run(_principal())
        # Only deletions (max-age 0), never a value.
        assert resp.cookies["stapel_jwt"].value == ""
        assert resp.cookies["stapel_jwt"]["max-age"] == 0
        assert resp.cookies["stapel_refresh_jwt"].value == ""
        assert resp.cookies["stapel_refresh_jwt"]["max-age"] == 0

    def test_non_staff_sees_the_form_again(self):
        resp, _, _ = self._run(_principal())
        assert resp.content == b"login-form-again"

    def test_refusal_message_does_not_confirm_the_password(self):
        view, form = _view_for(_principal())
        with (
            patch("stapel_core.django.jwt.login_views.login"),
            patch(LOGIN_PROVIDER),
            patch.object(JWTCookieLoginView, "form_invalid", return_value=HttpResponse()),
        ):
            view.form_valid(form)
        args, _ = form.add_error.call_args
        assert args[0] is None  # non-field error, exactly like a wrong password
        assert "staff account" in str(args[1].message)

    def test_staff_still_gets_tokens_and_cookies(self):
        view, form = _view_for(_principal(is_staff=True))
        with (
            patch("stapel_core.django.jwt.login_views.login") as mock_login,
            patch(LOGIN_PROVIDER) as provider,
            patch.object(LoginView, "form_valid", return_value=HttpResponse()),
        ):
            provider.create_tokens.return_value = ("acc.tok", "ref.tok")
            resp = view.form_valid(form)
        mock_login.assert_called_once()
        assert resp.cookies["stapel_jwt"].value == "acc.tok"
        assert resp.cookies["stapel_refresh_jwt"].value == "ref.tok"

    def test_superuser_without_is_staff_still_gets_tokens(self):
        view, form = _view_for(_principal(is_superuser=True))
        with (
            patch("stapel_core.django.jwt.login_views.login"),
            patch(LOGIN_PROVIDER) as provider,
            patch.object(LoginView, "form_valid", return_value=HttpResponse()),
        ):
            provider.create_tokens.return_value = ("acc.tok", "ref.tok")
            resp = view.form_valid(form)
        assert resp.cookies["stapel_jwt"].value == "acc.tok"

    def test_permissive_form_on_a_subclass_does_not_reopen_the_hole(self):
        """The whole point of the second gate."""

        class Loosened(JWTCookieLoginView):
            authentication_form = AuthenticationForm

        assert Loosened().get_form_class() is AuthenticationForm

        view = Loosened()
        view.request = _request()
        form = MagicMock()
        form.get_user.return_value = _principal()
        with (
            patch("stapel_core.django.jwt.login_views.login") as mock_login,
            patch(LOGIN_PROVIDER) as provider,
            patch.object(Loosened, "form_invalid", return_value=HttpResponse()),
        ):
            view.form_valid(form)
        provider.create_tokens.assert_not_called()
        mock_login.assert_not_called()


# ---------------------------------------------------------------------------
# Defect 2 — the refresh endpoint re-minted from stale claims.
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    from stapel_core.django.jwt.provider import jwt_provider

    jwt_provider.reset()
    yield jwt_provider
    jwt_provider.reset()


@pytest.fixture
def refresh_allowed():
    with override_settings(JWT_REFRESH_ALLOWED=True):
        yield


class TestRefreshViewPassesTheDatabaseLoader:
    def test_loader_is_passed(self, refresh_allowed):
        from stapel_core.django.jwt.utils import load_user_by_uid

        with patch(VIEWS_PROVIDER) as prov:
            prov.refresh_access_token.return_value = "new.access.tok"
            resp = JWTRefreshView.as_view()(
                _request(cookies={"stapel_refresh_jwt": "ref.tok"})
            )
        assert resp.status_code == 200
        prov.refresh_access_token.assert_called_once_with("ref.tok", load_user_by_uid)


@pytest.mark.django_db
@pytest.mark.usefixtures("refresh_allowed")
class TestRevocationTakesEffectOnRefresh:
    """No patching of the view: it inherits the seam the middleware already had."""

    def _staff_user(self, username):
        user = get_user_model()(username=username, email=f"{username}@example.com", is_staff=True)
        user.set_password(PASSWORD)
        user.save()
        return user

    def _refresh(self, provider, user):
        from stapel_core.django.jwt.utils import serialize_user_to_jwt_data

        _, refresh = provider.create_tokens_from_data(serialize_user_to_jwt_data(user))
        return refresh

    def _post(self, refresh):
        return JWTRefreshView.as_view()(
            _request(cookies={"stapel_refresh_jwt": refresh})
        )

    def test_revoked_staff_flag_does_not_resurrect(self, provider):
        user = self._staff_user("refresh-demoted")
        refresh = self._refresh(provider, user)

        # Demote in the database, exactly as an admin revocation would.
        get_user_model().objects.filter(pk=user.pk).update(is_staff=False)

        resp = self._post(refresh)
        assert resp.status_code == 200
        new_access = json.loads(resp.content)["access_token"]
        claims = provider.validate_token(new_access)
        assert claims["is_staff"] is False, "a revoked staff flag came back on refresh"

    def test_staff_flag_survives_when_it_was_not_revoked(self, provider):
        user = self._staff_user("refresh-still-staff")
        resp = self._post(self._refresh(provider, user))
        assert resp.status_code == 200
        claims = provider.validate_token(json.loads(resp.content)["access_token"])
        assert claims["is_staff"] is True

    def test_deactivated_user_cannot_refresh(self, provider):
        user = self._staff_user("refresh-deactivated")
        refresh = self._refresh(provider, user)

        get_user_model().objects.filter(pk=user.pk).update(is_active=False)

        resp = self._post(refresh)
        assert resp.status_code == 401

    def test_deleted_user_cannot_refresh(self, provider):
        user = self._staff_user("refresh-deleted")
        refresh = self._refresh(provider, user)

        get_user_model().objects.filter(pk=user.pk).delete()

        assert self._post(refresh).status_code == 401


@pytest.mark.django_db
class TestLoadUserByUidRefusesDeactivated:
    """The one loader every refresh path shares, so no path can skip the rule."""

    def test_active_user_loads(self):
        from stapel_core.django.jwt.utils import load_user_by_uid

        user = get_user_model().objects.create(username="loader-active")
        assert load_user_by_uid(user.pk)["user_id"] == str(user.pk)

    def test_deactivated_user_does_not_load(self):
        from stapel_core.django.jwt.utils import load_user_by_uid

        user = get_user_model().objects.create(username="loader-off", is_active=False)
        assert load_user_by_uid(user.pk) is None


# ---------------------------------------------------------------------------
# Same class, one layer down: nothing in the JWT path enforced is_active.
#
# `django.contrib.auth.login()` does NOT check `user_can_authenticate()` —
# that lives in `authenticate()`, which every JWT path bypasses by design
# (the credential is a signature, not a password). So a deactivated account
# authenticated on every JWT path, and — because both `get_user()` overrides
# dropped Django's own check — kept a live session afterwards for as long as
# the session cookie lasted.
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestInactiveUserAuthenticatesNobody:
    def _closed_account(self, username):
        return get_user_model().objects.create(
            username=username, email=f"{username}@example.com", is_active=False
        )

    def _claims(self, user):
        return {
            "user_id": str(user.pk),
            "email": user.email,
            "username": user.username,
            "is_staff": False,
            "is_superuser": False,
        }

    def test_the_shared_seam_resolves_nobody(self):
        """One gate, so no caller — present or future — can skip it."""
        from stapel_core.django.jwt.utils import get_or_create_user_from_jwt

        user = self._closed_account("closed-seam")
        assert get_or_create_user_from_jwt(self._claims(user)) is None

    def test_active_user_still_resolves(self):
        from stapel_core.django.jwt.utils import get_or_create_user_from_jwt

        user = get_user_model().objects.create(
            username="open-seam", email="open-seam@example.com"
        )
        assert get_or_create_user_from_jwt(self._claims(user)).pk == user.pk

    def test_jwt_backend_authenticates_nobody(self, provider):
        from stapel_core.django.jwt.backends import JWTAuthBackend

        user = self._closed_account("closed-backend")
        access, _ = provider.create_tokens_from_data(self._claims(user))
        assert JWTAuthBackend().authenticate(None, jwt_token=access) is None

    def test_drf_authentication_class_authenticates_nobody(self, provider):
        from stapel_core.django.jwt.authentication import JWTCookieAuthentication

        user = self._closed_account("closed-drf")
        access, _ = provider.create_tokens_from_data(self._claims(user))
        req = _request(cookies={"stapel_jwt": access})
        assert JWTCookieAuthentication().authenticate(req) is None

    def test_session_backend_stops_resolving_a_deactivated_session(self):
        """The session outlived the account: get_user() ran on every request."""
        from stapel_core.django.jwt.session import EmailAuthBackend

        user = self._closed_account("closed-session")
        assert EmailAuthBackend().get_user(user.pk) is None

    def test_session_backend_still_resolves_an_active_user(self):
        from stapel_core.django.jwt.session import EmailAuthBackend

        user = get_user_model().objects.create(username="open-session")
        assert EmailAuthBackend().get_user(user.pk).pk == user.pk

    def test_jwt_backend_stops_resolving_a_deactivated_session(self):
        from stapel_core.django.jwt.backends import JWTAuthBackend

        user = self._closed_account("closed-backend-session")
        assert JWTAuthBackend().get_user(user.pk) is None
