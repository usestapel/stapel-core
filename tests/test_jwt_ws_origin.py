"""Tests for stapel_core.django.jwt.ws_origin — the WebSocket origin guard.

The guard exists because 0.44.0 taught the Channels handshake to read the JWT
cookie. A cookie is ambient authority: the browser attaches it to a handshake
started by any page, and WebSockets are protected by neither the same-origin
policy nor CORS. Without the allowlist, the cookie fix would be Cross-Site
WebSocket Hijacking — so the two ship together and the guard fails CLOSED.
"""

import pytest
from django.core import checks
from django.test import override_settings

from stapel_core.django.jwt import ws_origin as wo


def _ids(findings):
    return [f.id for f in findings]


class TestNormalizeOrigin:
    @pytest.mark.parametrize("raw,expected", [
        ("https://app.example.com", "https://app.example.com"),
        ("HTTPS://App.Example.COM", "https://app.example.com"),
        ("https://app.example.com:443", "https://app.example.com"),
        ("http://app.example.com:80/path", "http://app.example.com"),
        ("http://localhost:5173", "http://localhost:5173"),
        # A non-default port is IDENTITY, not noise: an allowlist entry of
        # 'studio.localhost' that never matched 'http://studio.localhost:8600'
        # is a real incident.
        ("https://app.example.com:8443", "https://app.example.com:8443"),
        # A browser's Origin never says wss, so a socket-scheme entry folds
        # onto the scheme it is served over rather than never matching.
        ("wss://app.example.com", "https://app.example.com"),
        ("ws://localhost:5173", "http://localhost:5173"),
    ])
    def test_normalizes(self, raw, expected):
        assert wo.normalize_origin(raw) == expected

    @pytest.mark.parametrize("raw", ["app.example.com", "", "   ", "https://"])
    def test_refuses_a_non_origin(self, raw):
        with pytest.raises(ValueError):
            wo.normalize_origin(raw)


class TestAllowlistResolution:
    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=["https://a.example.com"])
    def test_core_setting_is_canonical(self):
        assert wo.websocket_origin_allowlist() == {"https://a.example.com"}

    @override_settings(
        STAPEL_WS_ALLOWED_ORIGINS=[],
        STAPEL_REALTIME={"ALLOWED_ORIGINS": ["https://b.example.com"]},
    )
    def test_realtime_list_is_read_so_a_host_declares_it_once(self):
        """Two lists that can disagree is how two layers give contradictory
        verdicts about the same socket. Core reads realtime's, so
        stapel_chat.E014 and stapel_core.jwt.E001 agree by construction."""
        assert wo.websocket_origin_allowlist() == {"https://b.example.com"}

    @override_settings(
        STAPEL_WS_ALLOWED_ORIGINS=["https://a.example.com"],
        STAPEL_REALTIME={"ALLOWED_ORIGINS": ["https://b.example.com"]},
    )
    def test_core_setting_wins_when_both_are_declared(self):
        assert wo.websocket_origin_allowlist() == {"https://a.example.com"}

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[], STAPEL_REALTIME={})
    def test_nothing_declared_is_an_empty_allowlist_not_a_wildcard(self):
        assert wo.websocket_origin_allowlist() == set()
        assert wo.origin_allowed("https://anything.example.com") is False

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=["nonsense", "https://ok.example"])
    def test_malformed_entry_is_dropped_not_honoured(self):
        """A typo must not be the thing that decides there is no guard."""
        assert wo.websocket_origin_allowlist() == {"https://ok.example"}
        assert wo.origin_allowed("https://nonsense") is False

    def test_explicit_entries_bypass_settings(self):
        assert wo.websocket_origin_allowlist(["https://x.example"]) == {
            "https://x.example"
        }
        assert wo.websocket_origin_allowlist([]) == set()

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=["https://a.example.com"])
    def test_origin_allowed_edges(self):
        assert wo.origin_allowed("https://a.example.com") is True
        assert wo.origin_allowed("https://evil.example.net") is False
        assert wo.origin_allowed(None) is False
        assert wo.origin_allowed("") is False
        assert wo.origin_allowed("garbage") is False


class TestReachability:
    """E001 fires only where a browser can actually reach the socket with a
    cookie. An HTTP-only service never sees it."""

    @override_settings(
        ASGI_APPLICATION=None, INSTALLED_APPS=[], MIDDLEWARE=[],
        REST_FRAMEWORK={}, JWT_REFRESH_ALLOWED=False,
    )
    def test_http_only_service_is_not_reachable(self):
        assert wo.cookie_websocket_auth_reachable() is False

    @override_settings(
        ASGI_APPLICATION="proj.asgi.application", MIDDLEWARE=[],
        REST_FRAMEWORK={}, JWT_REFRESH_ALLOWED=False, INSTALLED_APPS=[],
    )
    def test_websockets_without_cookie_credentials_is_not_reachable(self):
        assert wo.cookie_websocket_auth_reachable() is False

    @override_settings(
        ASGI_APPLICATION="proj.asgi.application",
        REST_FRAMEWORK={"DEFAULT_AUTHENTICATION_CLASSES": [
            "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
        ]},
    )
    def test_drf_cookie_class_makes_it_reachable(self):
        assert wo.cookie_websocket_auth_reachable() is True

    @override_settings(
        INSTALLED_APPS=["stapel_realtime"], REST_FRAMEWORK={},
        MIDDLEWARE=["stapel_core.django.jwt.middleware.JWTAuthMiddleware"],
        ASGI_APPLICATION=None,
    )
    def test_http_jwt_middleware_makes_it_reachable(self):
        """That middleware's extractor reads cookies FIRST, so a browser
        talking to this service holds one."""
        assert wo.cookie_websocket_auth_reachable() is True

    @override_settings(
        INSTALLED_APPS=["channels"], REST_FRAMEWORK={}, MIDDLEWARE=[],
        JWT_REFRESH_ALLOWED=True, ASGI_APPLICATION=None,
    )
    def test_a_service_that_sets_the_cookies_makes_it_reachable(self):
        assert wo.cookie_websocket_auth_reachable() is True


_REACHABLE = dict(
    ASGI_APPLICATION="proj.asgi.application",
    REST_FRAMEWORK={"DEFAULT_AUTHENTICATION_CLASSES": [
        "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
    ]},
)


class TestCheck:
    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[], STAPEL_REALTIME={},
                       **_REACHABLE)
    def test_e001_when_cookie_auth_is_reachable_and_nothing_is_declared(self):
        findings = wo.check_websocket_origin_allowlist(None)
        assert _ids(findings) == ["stapel_core.jwt.E001"]
        assert findings[0].level >= checks.ERROR
        assert "STAPEL_WS_ALLOWED_ORIGINS" in findings[0].hint

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=["https://app.example.com"],
                       **_REACHABLE)
    def test_silent_once_an_allowlist_is_declared(self):
        assert wo.check_websocket_origin_allowlist(None) == []

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[],
                       STAPEL_REALTIME={"ALLOWED_ORIGINS": ["https://a.example"]},
                       **_REACHABLE)
    def test_realtime_declaration_clears_it_too(self):
        """Otherwise a stapel-realtime host gets two contradictory verdicts:
        realtime says guarded, core says unguarded, about one socket."""
        assert wo.check_websocket_origin_allowlist(None) == []

    @override_settings(
        ASGI_APPLICATION=None, INSTALLED_APPS=[], MIDDLEWARE=[],
        REST_FRAMEWORK={}, JWT_REFRESH_ALLOWED=False,
        STAPEL_WS_ALLOWED_ORIGINS=[], STAPEL_REALTIME={},
    )
    def test_http_only_service_is_never_blocked_by_it(self):
        assert wo.check_websocket_origin_allowlist(None) == []

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=["studio.localhost"],
                       **_REACHABLE)
    def test_e002_names_an_entry_that_can_never_match(self):
        findings = wo.check_websocket_origin_allowlist(None)
        assert _ids(findings) == ["stapel_core.jwt.E002"]
        assert "studio.localhost" in findings[0].msg

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[], STAPEL_REALTIME={},
                       SILENCED_SYSTEM_CHECKS=["stapel_core.jwt.E001"],
                       **_REACHABLE)
    def test_e001_cannot_be_silenced_by_a_blanket_line(self):
        from stapel_core.django.check_guard import is_security_critical

        assert is_security_critical("stapel_core.jwt.E001")
        findings = wo.check_websocket_origin_allowlist(None)
        assert _ids(findings) == ["stapel_core.jwt.E001"]

    def test_the_check_is_registered_at_boot(self):
        """CommonDjangoConfig.ready imports the module — the guard is not
        something a host has to remember to wire up."""
        from django.core.checks.registry import registry

        registered = {
            getattr(fn, "__name__", "") for fn in registry.get_checks()
        }
        assert "check_websocket_origin_allowlist" in registered
