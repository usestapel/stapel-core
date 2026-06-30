"""Tests for stapel_core.oauth — provider registry + auth-code flow helpers."""
from unittest.mock import patch, MagicMock

import pytest

from stapel_core import oauth
from stapel_core.oauth import (
    OAuthProvider,
    OAuthUserData,
    register_provider,
    get_provider,
    get_all_providers,
)


class _FakeProvider(OAuthProvider):
    id = "fake"
    display_name = "Fake"
    auth_url = "https://example.com/auth"
    token_url = "https://example.com/token"
    scope = "read"
    extra_params = {"access_type": "offline"}

    def get_user_data(self, access_token: str) -> OAuthUserData | None:
        if access_token == "bad":
            return None
        return OAuthUserData(id="42", email="u@example.com", username="u", avatar=None)


@pytest.fixture(autouse=True)
def _reset_registry():
    # Each test starts with a clean registry.
    saved = dict(oauth._registry)
    oauth._registry.clear()
    yield
    oauth._registry.clear()
    oauth._registry.update(saved)


class TestRegistry:
    def test_register_and_get_provider(self):
        p = _FakeProvider()
        register_provider(p)
        assert get_provider("fake") is p

    def test_get_provider_unknown_returns_none(self):
        assert get_provider("nope") is None

    def test_register_overwrites_same_id(self):
        p1, p2 = _FakeProvider(), _FakeProvider()
        register_provider(p1)
        register_provider(p2)
        assert get_provider("fake") is p2

    def test_get_all_providers(self):
        register_provider(_FakeProvider())
        assert [p.id for p in get_all_providers()] == ["fake"]


class TestAuthorizationUrl:
    def test_url_contains_required_params(self):
        url = _FakeProvider().get_authorization_url(
            client_id="cid", redirect_uri="https://app/cb", state="xyz"
        )
        assert url.startswith("https://example.com/auth?")
        for fragment in ("client_id=cid", "redirect_uri=", "state=xyz", "response_type=code", "access_type=offline"):
            assert fragment in url


class TestExchangeCode:
    def _resp(self, status, payload):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload
        return r

    def test_returns_access_token_on_success(self):
        with patch("requests.post", return_value=self._resp(200, {"access_token": "abc"})) as m:
            tok = _FakeProvider().exchange_code("cid", "sec", "code", "https://app/cb")
        assert tok == "abc"
        # posted to the token_url with grant_type=authorization_code
        _, kwargs = m.call_args
        assert kwargs["data"]["grant_type"] == "authorization_code"
        assert kwargs["data"]["code"] == "code"

    def test_returns_none_on_non_200(self):
        with patch("requests.post", return_value=self._resp(401, {})):
            assert _FakeProvider().exchange_code("cid", "sec", "code", "https://app/cb") is None

    def test_returns_none_when_token_missing(self):
        with patch("requests.post", return_value=self._resp(200, {"error": "x"})):
            assert _FakeProvider().exchange_code("cid", "sec", "code", "https://app/cb") is None


class TestGetUserData:
    def test_returns_normalized_data(self):
        data = _FakeProvider().get_user_data("good")
        assert data == OAuthUserData(id="42", email="u@example.com", username="u", avatar=None)

    def test_returns_none_on_failure(self):
        assert _FakeProvider().get_user_data("bad") is None
