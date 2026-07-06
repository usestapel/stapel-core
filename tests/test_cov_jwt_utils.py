"""Coverage tests for stapel_core.django.jwt.utils."""
import sys
import types
import uuid
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.test import RequestFactory, override_settings

from stapel_core.django.jwt import utils as jwt_utils

factory = RequestFactory()

# redirect() resolves URLs against ROOT_URLCONF; conftest leaves it empty ('').
# Register a minimal empty urlconf so path-string redirects work.
_URLCONF = "test_cov_jwt_utils_urlconf"
_mod = types.ModuleType(_URLCONF)
_mod.urlpatterns = []
sys.modules.setdefault(_URLCONF, _mod)


def _uid():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# load_jwt_config_from_settings
# ---------------------------------------------------------------------------

class TestLoadJWTConfigFromSettings:
    @override_settings(
        JWT_ALGORITHM="RS256",
        JWT_PRIVATE_KEY="PRIV-PEM",
        JWT_PUBLIC_KEY="PUB-PEM",
    )
    def test_rs256_direct_key_content(self):
        cfg = jwt_utils.load_jwt_config_from_settings()
        assert cfg.algorithm == "RS256"
        assert cfg.private_key == "PRIV-PEM"
        assert cfg.public_key == "PUB-PEM"
        assert cfg.secret_key == ""

    def test_rs256_key_paths(self, tmp_path):
        priv = tmp_path / "priv.pem"
        pub = tmp_path / "pub.pem"
        priv.write_text("PRIV-FROM-FILE")
        pub.write_text("PUB-FROM-FILE")
        with override_settings(
            JWT_ALGORITHM="RS256",
            JWT_PRIVATE_KEY_PATH=str(priv),
            JWT_PUBLIC_KEY_PATH=str(pub),
        ):
            cfg = jwt_utils.load_jwt_config_from_settings()
        assert cfg.private_key == "PRIV-FROM-FILE"
        assert cfg.public_key == "PUB-FROM-FILE"

    @override_settings(JWT_SECRET_KEY="django-insecure-default", DEBUG=False)
    def test_hs256_insecure_secret_raises_outside_debug(self):
        with pytest.raises(ImproperlyConfigured, match="JWT is configured for HS256"):
            jwt_utils.load_jwt_config_from_settings()

    @override_settings(JWT_SECRET_KEY="django-insecure-default", DEBUG=True)
    def test_hs256_insecure_secret_allowed_in_debug(self):
        cfg = jwt_utils.load_jwt_config_from_settings()
        assert cfg.secret_key == "django-insecure-default"

    @override_settings(JWT_ISSUER="https://auth.example.com")
    def test_jwks_url_derived_from_http_issuer(self):
        cfg = jwt_utils.load_jwt_config_from_settings()
        assert cfg.jwks_url == "https://auth.example.com/auth/.well-known/jwks.json"

    @override_settings(
        JWT_ISSUER="https://auth.example.com", STAPEL_AUTH_SERVICE_PREFIX=""
    )
    def test_jwks_url_without_prefix(self):
        cfg = jwt_utils.load_jwt_config_from_settings()
        assert cfg.jwks_url == "https://auth.example.com/.well-known/jwks.json"

    def test_defaults_from_conftest_settings(self):
        cfg = jwt_utils.load_jwt_config_from_settings()
        assert cfg.algorithm == "HS256"
        assert cfg.issuer == "stapel-auth"
        assert cfg.jwks_url is None


# ---------------------------------------------------------------------------
# load_user_by_uid / serialize_user_to_jwt_data
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestLoadUserByUid:
    def test_found_serializes_user(self):
        User = get_user_model()
        user = User.objects.create_user(
            username="lu1", email="lu1@example.com", phone="+15551234567"
        )
        data = jwt_utils.load_user_by_uid(user.pk)
        assert data["user_id"] == str(user.pk)
        assert data["email"] == "lu1@example.com"
        assert data["phone"] == "+15551234567"
        assert data["auth_type"] == "email"
        assert data["is_anonymous"] is False

    def test_not_found_returns_none(self):
        assert jwt_utils.load_user_by_uid(_uid()) is None

    def test_unexpected_error_returns_none(self):
        fake_model = MagicMock()
        fake_model.DoesNotExist = LookupError
        fake_model.objects.get.side_effect = RuntimeError("db down")
        with patch.object(jwt_utils, "_get_user_model", return_value=fake_model):
            assert jwt_utils.load_user_by_uid("whatever") is None


# ---------------------------------------------------------------------------
# serialize_user_to_jwt_data — staff_roles claim (admin-suite AS-2)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSerializeStaffRolesClaim:
    def test_staff_user_with_field_emits_sorted_claim(self):
        User = get_user_model()
        user = User.objects.create_user(username="sr1", is_staff=True)
        user.staff_roles = ["editor", "admin"]
        user.save()
        data = jwt_utils.serialize_user_to_jwt_data(user)
        assert data["staff_roles"] == ["admin", "editor"]

    def test_non_staff_user_has_no_claim(self):
        User = get_user_model()
        user = User.objects.create_user(username="sr2")
        user.staff_roles = ["admin"]
        user.save()
        data = jwt_utils.serialize_user_to_jwt_data(user)
        assert "staff_roles" not in data

    def test_superuser_with_empty_field_emits_empty_claim(self):
        User = get_user_model()
        user = User.objects.create_user(username="sr3", is_superuser=True)
        data = jwt_utils.serialize_user_to_jwt_data(user)
        assert data["staff_roles"] == []

    def test_model_without_field_omits_claim(self):
        # Custom user without the mixin field: no key at all (pre-AS-2).
        class _FakeUser:
            pk = "x"
            email = "f@example.com"
            username = "fake"
            is_staff = True
            is_superuser = False
            is_active = True

        data = jwt_utils.serialize_user_to_jwt_data(_FakeUser())
        assert "staff_roles" not in data


# ---------------------------------------------------------------------------
# _ensure_user_in_staff_group
# ---------------------------------------------------------------------------

class TestEnsureUserInStaffGroup:
    def test_error_returns_false(self):
        with patch(
            "stapel_core.django.groups.add_user_to_staff_group",
            side_effect=RuntimeError("boom"),
        ):
            assert jwt_utils._ensure_user_in_staff_group(MagicMock()) is False

    @pytest.mark.django_db
    def test_staff_user_added_to_group(self):
        User = get_user_model()
        user = User.objects.create_user(username="staff1", is_staff=True)
        assert jwt_utils._ensure_user_in_staff_group(user) is True
        assert user.groups.filter(name="Staff").exists()
        # Second call: already a member
        assert jwt_utils._ensure_user_in_staff_group(user) is False


# ---------------------------------------------------------------------------
# get_or_create_user_from_jwt
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestGetOrCreateUserFromJWT:
    def _data(self, **kwargs):
        data = {
            "user_id": _uid(),
            "email": "jwt@example.com",
            "username": "jwtuser",
            "is_staff": False,
            "is_superuser": False,
            "is_active": True,
        }
        data.update(kwargs)
        return data

    def test_missing_user_id_returns_none(self):
        assert jwt_utils.get_or_create_user_from_jwt({"email": "x@y.z"}) is None

    def test_existing_user_upgraded_from_jwt(self):
        User = get_user_model()
        user = User.objects.create_user(username="ex1", email="old@example.com")
        data = self._data(
            user_id=str(user.pk),
            email="new@example.com",
            is_staff=True,
            is_superuser=True,
            is_anonymous=True,
            auth_type="phone",
            phone="+15551234567",
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result.pk == user.pk
        result.refresh_from_db()
        assert result.is_staff is True
        assert result.is_superuser is True
        assert result.is_anonymous is True
        assert result.auth_type == "phone"
        assert result.phone == "+15551234567"

    def test_existing_user_permissions_replaced_downgraded(self):
        # Consumer mode (JWT_CREATE_USERS_FROM_TOKEN default True): auth is the
        # source of truth (в.3) — is_staff/is_superuser are REPLACED, so a
        # token with the flags cleared DOWNGRADES a local staff/superuser.
        User = get_user_model()
        user = User.objects.create_user(
            username="ex2", email="e2@example.com", is_staff=True, is_superuser=True
        )
        data = self._data(
            user_id=str(user.pk),
            email="e2@example.com",
            is_staff=False,
            is_superuser=False,
            is_active=False,
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        result.refresh_from_db()
        assert result.is_staff is False
        assert result.is_superuser is False
        # is_active IS synced (both directions)
        assert result.is_active is False

    def test_existing_user_roles_replaced_from_claim(self):
        User = get_user_model()
        user = User.objects.create_user(
            username="rr1", email="rr1@example.com", is_staff=True
        )
        user.staff_roles = ["admin"]
        user.save()
        data = self._data(
            user_id=str(user.pk),
            email="rr1@example.com",
            is_staff=True,
            staff_roles=["editor"],
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        result.refresh_from_db()
        assert result.staff_roles == ["editor"]

    def test_existing_user_roles_authoritative_empty_revokes(self):
        # Present-but-empty claim is authoritative "zero roles" — revocation
        # must land locally.
        User = get_user_model()
        user = User.objects.create_user(
            username="rr2", email="rr2@example.com", is_staff=True
        )
        user.staff_roles = ["admin", "editor"]
        user.save()
        data = self._data(
            user_id=str(user.pk),
            email="rr2@example.com",
            is_staff=True,
            staff_roles=[],
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        result.refresh_from_db()
        assert result.staff_roles == []

    def test_existing_user_roles_untouched_when_claim_absent(self):
        # A pre-AS-2 token carries no staff_roles key: roles must not change
        # (no downgrade AND no upgrade from silence); booleans still REPLACE.
        User = get_user_model()
        user = User.objects.create_user(
            username="rr3", email="rr3@example.com", is_staff=True
        )
        user.staff_roles = ["admin"]
        user.save()
        data = self._data(
            user_id=str(user.pk), email="rr3@example.com", is_staff=True
        )
        assert "staff_roles" not in data
        result = jwt_utils.get_or_create_user_from_jwt(data)
        result.refresh_from_db()
        assert result.staff_roles == ["admin"]
        assert result.is_staff is True

    @override_settings(JWT_CREATE_USERS_FROM_TOKEN=False)
    def test_existing_user_staff_attrs_not_written_in_auth_mode(self):
        # Auth-service/monolith mode: the local DB is canonical. A token must
        # never write staff booleans OR roles back into it (closes the
        # re-elevation-by-stale-token hole).
        User = get_user_model()
        user = User.objects.create_user(
            username="am1", email="am1@example.com", is_staff=True, is_superuser=True
        )
        user.staff_roles = ["admin"]
        user.save()
        data = self._data(
            user_id=str(user.pk),
            email="am1@example.com",
            is_staff=False,
            is_superuser=False,
            staff_roles=["editor"],
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        result.refresh_from_db()
        assert result.is_staff is True
        assert result.is_superuser is True
        assert result.staff_roles == ["admin"]

    def test_claim_attr_stamped_on_user_when_claim_present(self):
        from stapel_core.access.sources import CLAIM_ATTR

        User = get_user_model()
        user = User.objects.create_user(
            username="ca1", email="ca1@example.com", is_staff=True
        )
        data = self._data(
            user_id=str(user.pk),
            email="ca1@example.com",
            is_staff=True,
            staff_roles=["editor", "admin"],
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert getattr(result, CLAIM_ATTR) == ["editor", "admin"]

    def test_claim_attr_absent_when_claim_absent(self):
        from stapel_core.access.sources import CLAIM_ATTR

        User = get_user_model()
        user = User.objects.create_user(
            username="ca2", email="ca2@example.com", is_staff=True
        )
        data = self._data(user_id=str(user.pk), email="ca2@example.com", is_staff=True)
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert not hasattr(result, CLAIM_ATTR)

    def test_created_user_gets_roles_from_claim(self):
        data = self._data(
            username="cshadow",
            email="cshadow@example.com",
            is_staff=True,
            staff_roles=["editor"],
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result is not None
        result.refresh_from_db()
        assert result.staff_roles == ["editor"]

    def test_existing_staff_user_added_to_staff_group(self):
        User = get_user_model()
        user = User.objects.create_user(
            username="ex3", email="e3@example.com", is_staff=True
        )
        data = self._data(user_id=str(user.pk), email="e3@example.com", is_staff=True)
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result.groups.filter(name="Staff").exists()

    @override_settings(JWT_CREATE_USERS_FROM_TOKEN=False)
    def test_stale_jwt_rejected_when_creation_disabled(self):
        assert jwt_utils.get_or_create_user_from_jwt(self._data()) is None

    def test_creates_user_with_generated_username_and_normalized_phone(self):
        pk = _uid()
        data = self._data(
            user_id=pk, email="", username=None, phone="+1 415 555 2671"
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result is not None
        assert str(result.pk) == pk
        assert result.username.startswith("user_")
        assert result.email is None
        assert result.phone == "+14155552671"
        assert not result.has_usable_password()

    def test_creates_user_with_empty_phone_normalized_to_none(self):
        data = self._data(username="emptyphone", email="ep@example.com", phone="")
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result is not None
        assert result.phone is None

    def test_creates_user_with_invalid_phone_kept_as_is(self):
        data = self._data(username="badphone", email="bp@example.com", phone="12345")
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result is not None
        assert result.phone == "12345"

    def test_created_staff_user_added_to_staff_group(self):
        data = self._data(
            username="newstaff", email="ns@example.com", is_staff=True
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result is not None
        assert result.is_staff is True
        assert result.groups.filter(name="Staff").exists()

    def test_existing_user_matched_by_email_gets_new_pk(self):
        User = get_user_model()
        old = User.objects.create_user(username="oldpk", email="match@example.com")
        old_pk = old.pk
        new_pk = _uid()
        data = self._data(
            user_id=new_pk, email="match@example.com", username="oldpk"
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result is not None
        assert str(result.pk) == new_pk
        assert not User.objects.filter(pk=old_pk).exists()

    def test_existing_user_matched_by_phone_gets_new_pk(self):
        User = get_user_model()
        old = User.objects.create_user(
            username="phoneuser", email=None, phone="+14155552671"
        )
        old_pk = old.pk
        new_pk = _uid()
        data = self._data(
            user_id=new_pk, email=None, username="phoneuser", phone="+14155552671"
        )
        result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result is not None
        assert str(result.pk) == new_pk
        assert not User.objects.filter(pk=old_pk).exists()

    def _fake_model(self):
        fake = MagicMock()
        fake.DoesNotExist = type("DoesNotExist", (Exception,), {})
        return fake

    def test_existing_user_with_same_pk_returned_directly(self):
        fake = self._fake_model()
        fake.objects.get.side_effect = fake.DoesNotExist
        existing = MagicMock(pk="same-pk")
        fake.objects.filter.return_value.first.return_value = existing
        data = self._data(user_id="same-pk")
        # Make old_pk == pk so no delete/recreate happens
        with patch.object(jwt_utils, "_get_user_model", return_value=fake):
            result = jwt_utils.get_or_create_user_from_jwt(data)
        assert result is existing
        fake.objects.create_user.assert_not_called()

    def test_integrity_error_race_returns_concurrently_created_user(self):
        fake = self._fake_model()
        fake.objects.get.side_effect = fake.DoesNotExist
        racing_user = MagicMock()

        def filter_side_effect(**kwargs):
            m = MagicMock()
            m.first.return_value = racing_user if "pk" in kwargs else None
            return m

        fake.objects.filter.side_effect = filter_side_effect
        fake.objects.create_user.side_effect = IntegrityError("duplicate")
        with patch.object(jwt_utils, "_get_user_model", return_value=fake):
            result = jwt_utils.get_or_create_user_from_jwt(self._data())
        assert result is racing_user

    def test_integrity_error_without_existing_user_returns_none(self):
        fake = self._fake_model()
        fake.objects.get.side_effect = fake.DoesNotExist
        fake.objects.filter.return_value.first.return_value = None
        fake.objects.create_user.side_effect = IntegrityError("duplicate")
        with patch.object(jwt_utils, "_get_user_model", return_value=fake):
            result = jwt_utils.get_or_create_user_from_jwt(self._data())
        assert result is None

    def test_create_error_returns_none(self):
        fake = self._fake_model()
        fake.objects.get.side_effect = fake.DoesNotExist
        fake.objects.filter.return_value.first.return_value = None
        fake.objects.create_user.side_effect = RuntimeError("boom")
        with patch.object(jwt_utils, "_get_user_model", return_value=fake):
            result = jwt_utils.get_or_create_user_from_jwt(self._data())
        assert result is None

    def test_unexpected_lookup_error_returns_none(self):
        fake = self._fake_model()
        fake.objects.get.side_effect = RuntimeError("db exploded")
        with patch.object(jwt_utils, "_get_user_model", return_value=fake):
            result = jwt_utils.get_or_create_user_from_jwt(self._data())
        assert result is None


# ---------------------------------------------------------------------------
# _apply_jwt_fields
# ---------------------------------------------------------------------------

class TestApplyJwtFields:
    def test_applies_all_optional_fields(self):
        user = MagicMock()
        jwt_utils._apply_jwt_fields(
            user,
            {"is_anonymous": True, "auth_type": "phone"},
            phone="+14155552671",
        )
        assert user.is_anonymous is True
        assert user.auth_type == "phone"
        assert user.phone == "+14155552671"


# ---------------------------------------------------------------------------
# extract_jwt_from_request / set_jwt_cookies
# ---------------------------------------------------------------------------

class TestExtractAndSetCookies:
    def test_extract_from_cookies(self):
        req = factory.get("/")
        req.COOKIES = {"stapel_jwt": "acc", "stapel_refresh_jwt": "ref"}
        assert jwt_utils.extract_jwt_from_request(req) == ("acc", "ref")

    def test_extract_from_bearer_header(self):
        req = factory.get("/", HTTP_AUTHORIZATION="Bearer header-token")
        req.COOKIES = {}
        access, refresh = jwt_utils.extract_jwt_from_request(req)
        assert access == "header-token"
        assert refresh is None

    def test_extract_nothing(self):
        req = factory.get("/")
        req.COOKIES = {}
        assert jwt_utils.extract_jwt_from_request(req) == (None, None)

    def test_set_jwt_cookies_both_tokens(self):
        from django.http import HttpResponse

        resp = HttpResponse()
        jwt_utils.set_jwt_cookies(resp, "acc-token", "ref-token")
        assert resp.cookies["stapel_jwt"].value == "acc-token"
        assert resp.cookies["stapel_refresh_jwt"].value == "ref-token"
        assert resp.cookies["stapel_jwt"]["httponly"]

    def test_set_jwt_cookies_access_only(self):
        from django.http import HttpResponse

        resp = HttpResponse()
        jwt_utils.set_jwt_cookies(resp, "acc-token")
        assert "stapel_refresh_jwt" not in resp.cookies


# ---------------------------------------------------------------------------
# setup_centralized_admin_login / logout helpers
# ---------------------------------------------------------------------------

@pytest.mark.urls(_URLCONF)
class TestCentralizedAdminLogin:
    def test_login_redirects_with_next(self):
        site = MagicMock()
        jwt_utils.setup_centralized_admin_login(site, auth_service_prefix="auth")
        req = factory.get("/admin/login/", {"next": "/translate/admin/"})
        resp = site.login(req)
        assert resp.status_code == 302
        assert resp.url == "/auth/admin/login/?" + urlencode(
            {"next": "/translate/admin/"}
        )

    def test_login_default_next_from_url_prefix(self):
        site = MagicMock()
        with override_settings(URL_PREFIX="svc/"):
            jwt_utils.setup_centralized_admin_login(site)
        req = factory.get("/admin/login/")
        resp = site.login(req)
        assert resp.status_code == 302
        assert resp.url == "/auth/admin/login/?" + urlencode({"next": "/svc/admin/"})


@pytest.mark.urls(_URLCONF)
class TestAdminLogoutUrlPattern:
    def _make_request(self, method="get"):
        req = getattr(factory, method)("/svc/admin/logout/")
        req.COOKIES = {"stapel_jwt": "acc.tok", "stapel_refresh_jwt": "ref.tok"}
        req.session = MagicMock()
        return req

    def test_pattern_route(self):
        pattern = jwt_utils.get_admin_logout_urlpattern(
            url_prefix="svc/", auth_service_prefix="auth"
        )
        assert str(pattern.pattern) == "svc/admin/logout/"
        assert pattern.name == "admin-logout"

    def test_get_logs_out_and_redirects(self):
        with patch("stapel_core.django.jwt.views.jwt_provider") as provider:
            pattern = jwt_utils.get_admin_logout_urlpattern(
                url_prefix="svc/", auth_service_prefix="auth"
            )
            req = self._make_request("get")
            resp = pattern.callback(req)
        assert resp.status_code == 302
        assert resp.url == "/auth/admin/login/"
        assert provider.blacklist_token.call_count == 2
        assert resp.cookies["stapel_jwt"]["max-age"] == 0
        assert resp.cookies["stapel_refresh_jwt"]["max-age"] == 0
        req.session.flush.assert_called_once()

    def test_post_logs_out_and_redirects(self):
        with patch("stapel_core.django.jwt.views.jwt_provider"):
            pattern = jwt_utils.get_admin_logout_urlpattern(auth_service_prefix="auth")
            req = self._make_request("post")
            resp = pattern.callback(req)
        assert resp.status_code == 302
        assert resp.url == "/auth/admin/login/"


class TestDeprecatedSetupLogout:
    def test_warns_deprecation(self):
        with pytest.warns(DeprecationWarning, match="deprecated"):
            jwt_utils.setup_centralized_admin_logout(MagicMock())


# ---------------------------------------------------------------------------
# reset_sequences_for_models
# ---------------------------------------------------------------------------

class TestResetSequences:
    def test_skips_non_autofield_models(self):
        User = get_user_model()  # UUID primary key
        mock_conn = MagicMock()
        with patch("django.db.connection", mock_conn):
            jwt_utils.reset_sequences_for_models(User)
        mock_conn.cursor.assert_not_called()

    def test_skips_model_without_pk_field(self):
        from types import SimpleNamespace

        fake_model = SimpleNamespace(_meta=SimpleNamespace(pk=None))
        mock_conn = MagicMock()
        with patch("django.db.connection", mock_conn):
            jwt_utils.reset_sequences_for_models(fake_model)
        mock_conn.cursor.assert_not_called()

    def test_resets_sequence_when_behind(self):
        from django.contrib.auth.models import Group

        mock_conn = MagicMock()
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [(10,), (5,)]
        with patch("django.db.connection", mock_conn):
            jwt_utils.reset_sequences_for_models(Group)
        executed = [call.args[0] for call in cur.execute.call_args_list]
        assert any("setval" in sql for sql in executed)

    def test_no_reset_when_sequence_current(self):
        from django.contrib.auth.models import Group

        mock_conn = MagicMock()
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [(None,), (0,)]
        with patch("django.db.connection", mock_conn):
            jwt_utils.reset_sequences_for_models(Group)
        executed = [call.args[0] for call in cur.execute.call_args_list]
        assert not any("setval" in sql for sql in executed)

    def test_all_models_when_none_given(self):
        mock_conn = MagicMock()
        cur = mock_conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (0,)
        with patch("django.db.connection", mock_conn):
            jwt_utils.reset_sequences_for_models()
        # At least one autofield model exists in the test project
        assert mock_conn.cursor.called

    @pytest.mark.django_db
    def test_sqlite_sequence_error_is_swallowed(self):
        # SQLite has no PostgreSQL sequences: the SELECT last_value query fails
        # and the error must be logged, not raised.
        from django.contrib.auth.models import Group

        jwt_utils.reset_sequences_for_models(Group)
