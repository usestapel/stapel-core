"""An access token is a bearer credential for the client it was minted for.

Nothing in this seam used to be able to ask which client that was:
``get_user_data(self, access_token)`` carried no provider configuration, so a
service that accepts a token straight from a request body and looks up the
profile behind it had no way to tell a token minted for ITS OAuth client from
one minted for somebody else's app against the same victim's provider account.
Resolving the second as an identity is a login takeover.

``check_audience`` is the gate, and every path through it that is not a
positive proof — no mechanism, nothing configured to compare against, a
mismatch, or an exception inside the verifier — returns a refusal. A
verification step that fails open is not a verification step.
"""
from unittest.mock import patch

import pytest

from stapel_core import oauth
from stapel_core.oauth import (
    AUDIENCE_MISMATCH,
    AUDIENCE_UNPINNED,
    AUDIENCE_UNVERIFIABLE,
    OAuthClientConfig,
    OAuthProvider,
    OAuthUserData,
    check_audience,
    fetch_user_data,
)

WEB = "web-client-id"
IOS = "ios-client-id"
ANDROID = "android-client-id"
STRANGER = "somebody-elses-client-id"


class _SilentProvider(OAuthProvider):
    """A provider nobody taught to verify — the default every custom one gets."""

    id = "silent"
    display_name = "Silent"
    auth_url = "https://example.com/auth"
    token_url = "https://example.com/token"
    scope = "read"
    extra_params = {}

    def get_user_data(self, access_token, config=None):
        return OAuthUserData(id="1", email=None, username=None, avatar=None)


class _VerifyingProvider(_SilentProvider):
    """One with a real mechanism: the token names the client that minted it."""

    id = "verifying"
    verifies_audience = True

    def verify_audience(self, access_token, config):
        # Token format for the test: "<audience>:<anything>".
        minted_for = str(access_token).split(":", 1)[0]
        return minted_for in config.accepted_audiences


class _ExplodingProvider(_SilentProvider):
    id = "exploding"
    verifies_audience = True

    def verify_audience(self, access_token, config):
        raise RuntimeError("introspection endpoint is down")


class _LegacyProvider(OAuthProvider):
    """Written against the pre-0.42 one-argument signature."""

    id = "legacy"
    display_name = "Legacy"
    auth_url = "https://example.com/auth"
    token_url = "https://example.com/token"
    scope = "read"
    extra_params = {}

    def get_user_data(self, access_token):
        return OAuthUserData(id=access_token, email=None, username=None, avatar=None)


class _KwargsProvider(OAuthProvider):
    id = "kwargs"
    display_name = "Kwargs"
    auth_url = "https://example.com/auth"
    token_url = "https://example.com/token"
    scope = "read"
    extra_params = {}

    def get_user_data(self, access_token, **kw):
        return OAuthUserData(
            id=str(kw.get("config") and kw["config"].client_id),
            email=None,
            username=None,
            avatar=None,
        )


@pytest.fixture(autouse=True)
def _clear_signature_cache():
    oauth._ACCEPTS_CONFIG.clear()
    yield
    oauth._ACCEPTS_CONFIG.clear()


def _config(*audiences, client_id=WEB, client_secret="s3cret"):
    return OAuthClientConfig(
        client_id=client_id,
        client_secret=client_secret,
        accepted_audiences=tuple(audiences),
    )


class TestRefuseIsTheDefault:
    def test_a_provider_with_no_mechanism_refuses(self):
        """The whole point: silence is not consent."""
        assert (
            check_audience(_SilentProvider(), f"{WEB}:tok", _config(WEB))
            == AUDIENCE_UNVERIFIABLE
        )

    def test_the_base_class_hook_returns_false(self):
        assert _SilentProvider().verifies_audience is False
        assert _SilentProvider().verify_audience("anything", _config(WEB)) is False

    def test_nothing_pinned_refuses(self):
        assert (
            check_audience(_VerifyingProvider(), f"{WEB}:tok", _config())
            == AUDIENCE_UNPINNED
        )

    def test_no_config_at_all_refuses(self):
        assert (
            check_audience(_VerifyingProvider(), f"{WEB}:tok", None)
            == AUDIENCE_UNPINNED
        )

    def test_a_verifier_that_raises_refuses(self):
        """Fail closed: an outage must not become an open door."""
        assert (
            check_audience(_ExplodingProvider(), f"{WEB}:tok", _config(WEB))
            == AUDIENCE_UNVERIFIABLE
        )

    def test_a_verifier_that_says_no_refuses(self):
        assert (
            check_audience(_VerifyingProvider(), f"{STRANGER}:tok", _config(WEB))
            == AUDIENCE_MISMATCH
        )


class TestAcceptingTheRightToken:
    def test_our_own_client_passes(self):
        assert check_audience(_VerifyingProvider(), f"{WEB}:tok", _config(WEB)) is None

    def test_every_listed_client_passes(self):
        """The mobile case: one project, separate Web / iOS / Android IDs.

        A single-value check would have refused every native sign-in, which
        is why the pin is a list.
        """
        config = _config(WEB, IOS, ANDROID)
        for audience in (WEB, IOS, ANDROID):
            assert check_audience(_VerifyingProvider(), f"{audience}:tok", config) is None

    def test_a_client_outside_the_list_still_refuses(self):
        config = _config(WEB, IOS, ANDROID)
        assert (
            check_audience(_VerifyingProvider(), f"{STRANGER}:tok", config)
            == AUDIENCE_MISMATCH
        )

    def test_the_configured_client_is_not_implicitly_accepted(self):
        """`client_id` is credentials, not a pin — only the list decides."""
        config = _config(IOS, client_id=WEB)
        assert (
            check_audience(_VerifyingProvider(), f"{WEB}:tok", config)
            == AUDIENCE_MISMATCH
        )


class TestConfigReachesProvidersThatWantIt:
    def test_a_new_style_provider_receives_the_config(self):
        config = _config(WEB)
        with patch.object(
            _SilentProvider, "get_user_data", autospec=True
        ) as get_user_data:
            get_user_data.return_value = None
            fetch_user_data(_SilentProvider(), "tok", config)
        assert get_user_data.call_args.kwargs["config"] is config

    def test_a_legacy_provider_is_called_with_one_argument(self):
        """Third-party providers predate this signature and must keep working."""
        result = fetch_user_data(_LegacyProvider(), "tok", _config(WEB))
        assert result.id == "tok"

    def test_a_kwargs_provider_receives_the_config(self):
        result = fetch_user_data(_KwargsProvider(), "tok", _config(WEB))
        assert result.id == WEB

    def test_config_is_optional(self):
        assert fetch_user_data(_LegacyProvider(), "tok").id == "tok"

    def test_the_signature_answer_is_cached_per_class(self):
        fetch_user_data(_LegacyProvider(), "tok")
        assert oauth._ACCEPTS_CONFIG[_LegacyProvider] is False
        fetch_user_data(_SilentProvider(), "tok")
        assert oauth._ACCEPTS_CONFIG[_SilentProvider] is True


class TestClientConfig:
    def test_defaults_are_empty_and_therefore_refusing(self):
        config = OAuthClientConfig()
        assert config.client_id == ""
        assert config.client_secret == ""
        assert config.accepted_audiences == ()
        assert check_audience(_VerifyingProvider(), "t", config) == AUDIENCE_UNPINNED

    def test_it_is_frozen(self):
        with pytest.raises(Exception):
            OAuthClientConfig().client_id = "mutated"
