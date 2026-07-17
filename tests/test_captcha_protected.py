"""End-to-end tests for @captcha_protected — every level on a test view."""
from unittest import mock

from django.test import override_settings

from stapel_core.captcha import CaptchaVerifier, NoopVerifier
from stapel_core.captcha.policy import (
    LEVEL_INTERACTIVE,
    LEVEL_INTERACTIVE_RATELIMIT,
    LEVEL_INVISIBLE,
)
from stapel_core.django.captcha import (
    ERR_400_CAPTCHA_INVALID,
    ERR_400_CAPTCHA_REQUIRED,
    ERR_403_NETWORK_BLOCKED,
    captcha_protected,
)
from stapel_core.netintel import IpProfile


class _Req:
    def __init__(self, ip="192.0.2.1", token=None, header_token=None):
        self.META = {"REMOTE_ADDR": ip}
        self.headers = {}
        if header_token:
            self.headers["X-Captcha-Token"] = header_token
        self.data = {}
        if token:
            self.data["captcha_token"] = token


class _View:
    @captcha_protected(action="register")
    def post(self, request):
        return "view-ok"


@captcha_protected(action="register")
def _function_view(request):
    return "fbv-ok"


class LevelAwareVerifier(CaptchaVerifier):
    """Fake backend opting into the level kwarg."""

    def __init__(self, secret=None, result=True):
        super().__init__(secret)
        self.result = result
        self.seen = []  # (token, ip, level)

    def verify(self, token, ip=None, *, level=None):
        self.seen.append((token, ip, level))
        return self.result


def _with_kind(kind):
    return mock.patch(
        "stapel_core.netintel.classify_ip",
        return_value=IpProfile(ip="192.0.2.1", kind=kind),
    )


def _with_verifier(verifier):
    return mock.patch(
        "stapel_core.django.captcha.get_verifier", return_value=verifier,
    )


# ---------------------------------------------------------------------------
# Levels end-to-end
# ---------------------------------------------------------------------------


def test_level_none_passes_without_backend():
    request = _Req()
    with override_settings(STAPEL_CAPTCHA={
        "CHALLENGE_MATRIX": {"unknown": "none"},
    }):
        assert _View().post(request) == "view-ok"
    assert request.stapel_challenge_level == "none"


def test_level_block_returns_403_with_registered_error_key():
    request = _Req()
    with override_settings(STAPEL_CAPTCHA={
        "CHALLENGE_MATRIX": {"unknown": "block"},
    }):
        response = _View().post(request)
    assert response.status_code == 403
    assert response.data["localizable_error"] == ERR_403_NETWORK_BLOCKED
    # key is registered — the human-readable text is not the raw key
    assert response.data["error"] != ERR_403_NETWORK_BLOCKED
    assert request.stapel_challenge_level == "block"


def test_invisible_with_noop_backend_passes():
    # captcha unconfigured (NoopVerifier) + default matrix → pass, like today
    request = _Req()
    assert _View().post(request) == "view-ok"
    assert request.stapel_challenge_level == LEVEL_INVISIBLE


def test_invisible_with_configured_backend_verifies_token():
    verifier = LevelAwareVerifier()
    request = _Req(token="tok-1")
    with _with_verifier(verifier):
        assert _View().post(request) == "view-ok"
    assert verifier.seen == [("tok-1", "192.0.2.1", LEVEL_INVISIBLE)]


def test_interactive_level_reaches_backend():
    verifier = LevelAwareVerifier()
    request = _Req(token="tok-2")
    with _with_kind("datacenter"), _with_verifier(verifier):
        assert _View().post(request) == "view-ok"
    assert verifier.seen[0][2] == LEVEL_INTERACTIVE
    assert request.stapel_challenge_level == LEVEL_INTERACTIVE


def test_interactive_ratelimit_sets_request_attribute_for_middleware():
    verifier = LevelAwareVerifier()
    request = _Req(token="tok-3")
    with _with_kind("tor"), _with_verifier(verifier):
        assert _View().post(request) == "view-ok"
    # rate limiting is NOT done here — the level is exposed for middleware
    assert request.stapel_challenge_level == LEVEL_INTERACTIVE_RATELIMIT
    assert verifier.seen[0][2] == LEVEL_INTERACTIVE_RATELIMIT


def test_missing_token_with_configured_backend_400():
    verifier = LevelAwareVerifier()
    request = _Req()
    with _with_verifier(verifier):
        response = _View().post(request)
    assert response.status_code == 400
    assert response.data["localizable_error"] == ERR_400_CAPTCHA_REQUIRED
    assert verifier.seen == []


def test_invalid_token_400():
    verifier = LevelAwareVerifier(result=False)
    request = _Req(token="bad")
    with _with_verifier(verifier):
        response = _View().post(request)
    assert response.status_code == 400
    assert response.data["localizable_error"] == ERR_400_CAPTCHA_INVALID


def test_token_from_header():
    verifier = LevelAwareVerifier()
    request = _Req(header_token="tok-header")
    with _with_verifier(verifier):
        assert _View().post(request) == "view-ok"
    assert verifier.seen[0][0] == "tok-header"


def test_function_based_view_supported():
    request = _Req()
    assert _function_view(request) == "fbv-ok"
    assert request.stapel_challenge_level == LEVEL_INVISIBLE


def test_action_override_applies_through_decorator():
    request = _Req()
    with override_settings(STAPEL_CAPTCHA={
        "ACTION_OVERRIDES": {"register": {"unknown": "block"}},
    }):
        response = _View().post(request)
    assert response.status_code == 403


def test_policy_error_fails_open_to_invisible():
    class _Broken:
        def level_for(self, request, action):
            raise RuntimeError("boom")

    request = _Req()
    with mock.patch(
        "stapel_core.captcha.policy.get_challenge_policy", return_value=_Broken(),
    ), mock.patch(
        "stapel_core.django.captcha.get_verifier", return_value=NoopVerifier(),
    ):
        assert _View().post(request) == "view-ok"
    assert request.stapel_challenge_level == LEVEL_INVISIBLE


def test_decision_is_logged(caplog):
    request = _Req()
    with caplog.at_level("INFO", logger="stapel_core.django.captcha"):
        _View().post(request)
    decision = [r for r in caplog.records if "captcha decision" in r.getMessage()]
    assert len(decision) == 1
    message = decision[0].getMessage()
    assert "ip_kind=unknown" in message
    assert "action=register" in message
    assert "level=invisible" in message
    assert "allowed=True" in message


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_namespaced_settings_configure_verifier():
    from stapel_core.captcha import HcaptchaVerifier
    from stapel_core.django.captcha import get_verifier

    with override_settings(STAPEL_CAPTCHA={"BACKEND": "hcaptcha", "SECRET": "ns"}):
        verifier = get_verifier()
    assert isinstance(verifier, HcaptchaVerifier)
    assert verifier.secret == "ns"


def test_flat_settings_are_ignored():
    """The retired flat spelling must not configure anything."""
    from stapel_core.captcha import NoopVerifier as Noop
    from stapel_core.django.captcha import get_verifier

    with override_settings(CAPTCHA_BACKEND="turnstile", CAPTCHA_SECRET="s"):
        assert isinstance(get_verifier(), Noop)


def test_namespaced_secret_alone_enables_captcha():
    from stapel_core.captcha import NoopVerifier as Noop
    from stapel_core.django.captcha import get_verifier

    with override_settings(STAPEL_CAPTCHA={"SECRET": "s", "BACKEND": "turnstile"}):
        assert not isinstance(get_verifier(), Noop)
    # and without any secret anywhere → still disabled
    assert isinstance(get_verifier(), Noop)
