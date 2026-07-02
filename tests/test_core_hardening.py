"""Tests for the fork-free/hardening batch: CSRF policy, fail-closed
blacklist, AppSettings namespaces, schema autoload, user base class."""
import pytest
from django.test import RequestFactory, override_settings

from stapel_core.conf import AppSettings
from stapel_core.django.jwt.middleware import CsrfExemptAPIMiddleware


# ---------------------------------------------------------------------------
# CSRF: cookie-auth browser requests are NOT blanket-exempt
# ---------------------------------------------------------------------------


def _request(path="/x/api/thing/", cookies=None, headers=None):
    rf = RequestFactory()
    request = rf.post(path, **(headers or {}))
    for k, v in (cookies or {}).items():
        request.COOKIES[k] = v
    return request


def _exempt(request) -> bool:
    CsrfExemptAPIMiddleware(lambda r: None).process_request(request)
    return getattr(request, "_dont_enforce_csrf_checks", False)


def test_csrf_header_token_client_exempt():
    request = _request(headers={"HTTP_AUTHORIZATION": "Bearer abc"})
    assert _exempt(request) is True


def test_csrf_service_key_client_exempt():
    request = _request(headers={"HTTP_X_API_KEY": "svc"})
    assert _exempt(request) is True


def test_csrf_anonymous_api_client_exempt():
    assert _exempt(_request()) is True


def test_csrf_cookie_session_not_exempt():
    """A browser holding only the JWT cookie is CSRF-able — keep protection."""
    request = _request(cookies={"stapel_jwt": "tok"})
    assert _exempt(request) is False


def test_csrf_cookie_session_with_xhr_header_exempt():
    request = _request(
        cookies={"stapel_jwt": "tok"},
        headers={"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"},
    )
    assert _exempt(request) is True


def test_csrf_non_api_untouched():
    request = _request(path="/admin/login/")
    assert _exempt(request) is False


# ---------------------------------------------------------------------------
# Token blacklist fails closed
# ---------------------------------------------------------------------------


def test_blacklist_fails_closed_when_cache_down(monkeypatch):
    from django.core.cache import cache

    from stapel_core.core.token_blacklist import TokenBlacklist

    def boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(cache, "get", boom)
    assert TokenBlacklist().is_blacklisted("some-jti") is True
    with override_settings(STAPEL_BLACKLIST_FAIL_OPEN=True):
        assert TokenBlacklist().is_blacklisted("some-jti") is False


# ---------------------------------------------------------------------------
# AppSettings namespace
# ---------------------------------------------------------------------------


def test_app_settings_resolution_order(monkeypatch):
    conf = AppSettings("STAPEL_TESTNS", defaults={"COLOR": "blue", "SIZE": 1})
    assert conf.COLOR == "blue"

    conf.reload()
    monkeypatch.setenv("COLOR", "green")
    assert conf.COLOR == "green"  # env beats default

    conf.reload()
    with override_settings(COLOR="red"):
        assert conf.COLOR == "red"  # flat setting beats env

    conf.reload()
    with override_settings(STAPEL_TESTNS={"COLOR": "black"}, COLOR="red"):
        assert conf.COLOR == "black"  # namespace beats everything
    conf.reload()


def test_app_settings_import_strings():
    conf = AppSettings(
        "STAPEL_TESTNS2",
        defaults={"BACKEND": "stapel_core.bus.backends.memory.MemoryBus"},
        import_strings=("BACKEND",),
    )
    from stapel_core.bus.backends.memory import MemoryBus

    assert conf.BACKEND is MemoryBus
    with override_settings(STAPEL_TESTNS2={"BACKEND": "stapel_core.conf.AppSettings"}):
        conf.reload()
        assert conf.BACKEND is AppSettings
    conf.reload()


def test_app_settings_unknown_key():
    conf = AppSettings("STAPEL_TESTNS3", defaults={})
    with pytest.raises(AttributeError):
        conf.NOPE


# ---------------------------------------------------------------------------
# Schema autoload + user base
# ---------------------------------------------------------------------------


def test_schema_autoload_is_idempotent():
    from stapel_core.comm.schemas import autoload_schemas, reset_autoload

    reset_autoload()
    first = autoload_schemas()
    assert autoload_schemas() == 0  # second call is a no-op
    assert first >= 0
    reset_autoload()


def test_abstract_user_base_exported():
    from stapel_core.django.users.models import AbstractStapelUser, User

    assert issubclass(User, AbstractStapelUser)
    assert AbstractStapelUser._meta.abstract


@pytest.mark.django_db
def test_default_user_related_names_preserved():
    """Templated related_names must render to the historical values."""
    from stapel_core.django.users.models import User

    assert User._meta.get_field("groups").remote_field.related_name == "users_user_set"
    assert (
        User._meta.get_field("user_permissions").remote_field.related_name
        == "users_user_permissions_set"
    )
