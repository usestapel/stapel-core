"""Tests for stapel_core.django.jwt.channels — Channels JWT auth middleware.

Covers: token extraction from all four channels (Authorization header,
Sec-WebSocket-Protocol subprotocol in both shapes, ?token= query param, and
the JWT cookie) and their precedence; the full authenticate/reject flow
(valid / expired / missing / blacklisted token, banned user); rejection with
close code 4401 before accept; silent (no-error-log) rejection on exceptions;
non-websocket pass-through; and the optional-dependency contract.

The class of defect this file exists to prevent
-----------------------------------------------
Every test below the ``TestBrowserCookieHandshake`` heading authenticates the
way a BROWSER actually does: the JWT cookie in the handshake headers and no
``Authorization`` header at all. Until 0.44.1 every test in this file drove a
path a browser cannot take — a header ``new WebSocket()`` cannot set — so the
suite was green while the only handshake that mattered closed 4401 in
production for months. A green test over an impossible client proves nothing.
"""

import asyncio
import importlib
import logging
import subprocess
import sys

import pytest
from django.test import override_settings

from stapel_core.django.jwt import channels as ch


# ---------------------------------------------------------------------------
# ASGI test doubles
# ---------------------------------------------------------------------------

def _run(coro):
    """Drive a coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


def _ws_scope(headers=None, subprotocols=None, query_string=b""):
    scope = {"type": "websocket", "query_string": query_string}
    if headers is not None:
        scope["headers"] = headers
    if subprotocols is not None:
        scope["subprotocols"] = subprotocols
    return scope


ALLOWED = "https://app.example.com"
EVIL = "https://evil.example.net"


def _browser_scope(cookie="stapel_jwt=cookie.tok.en", origin=ALLOWED, extra=()):
    """A handshake shaped like the one a real browser sends.

    A cookie header the browser attached by itself, an Origin header it always
    sends, and — deliberately — NO Authorization header, because
    ``new WebSocket()`` cannot set one.
    """
    headers = [(b"cookie", cookie.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    headers.extend(extra)
    scope = _ws_scope(headers=headers)
    assert not any(name == b"authorization" for name, _ in headers)
    return scope


class _Sender:
    """Collects messages the app sends back."""

    def __init__(self):
        self.sent = []

    async def __call__(self, message):
        self.sent.append(message)


def _connect_receiver():
    """A receive() that yields a single websocket.connect then blocks-not-used."""
    async def receive():
        return {"type": "websocket.connect"}
    return receive


class _RecordingInner:
    """Inner ASGI app that records the scope it was called with."""

    def __init__(self):
        self.called = False
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.scope = scope
        await send({"type": "websocket.accept"})


# ---------------------------------------------------------------------------
# Token extraction (pure functions)
# ---------------------------------------------------------------------------

class TestExtractToken:
    def test_authorization_header(self):
        scope = _ws_scope(headers=[(b"authorization", b"Bearer abc.def.ghi")])
        assert ch._extract_token(scope) == "abc.def.ghi"

    def test_authorization_header_case_insensitive_scheme(self):
        scope = _ws_scope(headers=[(b"authorization", b"bearer tok")])
        assert ch._extract_token(scope) == "tok"

    def test_authorization_non_bearer_ignored(self):
        scope = _ws_scope(headers=[(b"authorization", b"Basic xyz")])
        assert ch._extract_token(scope) is None

    def test_subprotocol_dotted_shape(self):
        # "<scheme>.<token>" — split on the FIRST dot, JWT dots preserved.
        scope = _ws_scope(subprotocols=["bearer.aaa.bbb.ccc"])
        assert ch._extract_token(scope) == "aaa.bbb.ccc"

    def test_subprotocol_pair_shape(self):
        scope = _ws_scope(subprotocols=["bearer", "aaa.bbb.ccc"])
        assert ch._extract_token(scope) == "aaa.bbb.ccc"

    def test_subprotocol_access_token_scheme(self):
        scope = _ws_scope(subprotocols=["access_token", "tok"])
        assert ch._extract_token(scope) == "tok"

    def test_subprotocol_from_raw_header(self):
        # No scope["subprotocols"] key — parse Sec-WebSocket-Protocol header.
        scope = _ws_scope(headers=[(b"sec-websocket-protocol", b"bearer, tok")])
        assert ch._extract_token(scope) == "tok"

    def test_subprotocol_unknown_scheme_ignored(self):
        scope = _ws_scope(subprotocols=["graphql-ws"])
        assert ch._extract_token(scope) is None

    def test_query_param(self):
        scope = _ws_scope(query_string=b"token=aaa.bbb.ccc")
        assert ch._extract_token(scope) == "aaa.bbb.ccc"

    def test_query_param_among_others(self):
        scope = _ws_scope(query_string=b"foo=1&token=tok&bar=2")
        assert ch._extract_token(scope) == "tok"

    def test_missing_everywhere(self):
        assert ch._extract_token(_ws_scope()) is None

    # precedence: header > subprotocol > query
    def test_precedence_header_beats_subprotocol_and_query(self):
        scope = _ws_scope(
            headers=[(b"authorization", b"Bearer HEADER")],
            subprotocols=["bearer", "SUBPROTO"],
            query_string=b"token=QUERY",
        )
        assert ch._extract_token(scope) == "HEADER"

    def test_precedence_subprotocol_beats_query(self):
        scope = _ws_scope(
            subprotocols=["bearer", "SUBPROTO"],
            query_string=b"token=QUERY",
        )
        assert ch._extract_token(scope) == "SUBPROTO"


# ---------------------------------------------------------------------------
# The cookie channel — the one a browser actually has (0.44.1)
# ---------------------------------------------------------------------------

class TestExtractCookie:
    """A browser cannot set Authorization on new WebSocket(); it CAN send the
    httpOnly JWT cookie, because it attaches that by itself."""

    def test_jwt_cookie_is_read(self):
        # THE regression test: on 0.43.0 this returns None and every browser
        # handshake closes 4401.
        scope = _ws_scope(headers=[(b"cookie", b"stapel_jwt=aaa.bbb.ccc")])
        assert ch._extract_token(scope) == "aaa.bbb.ccc"

    def test_jwt_cookie_among_other_cookies(self):
        scope = _ws_scope(headers=[(
            b"cookie",
            b"sessionid=xyz; stapel_jwt=aaa.bbb.ccc; csrftoken=q",
        )])
        assert ch._extract_token(scope) == "aaa.bbb.ccc"

    def test_source_is_reported_as_cookie(self):
        scope = _ws_scope(headers=[(b"cookie", b"stapel_jwt=tok")])
        assert ch._extract_credential(scope) == ("tok", None, ch.SOURCE_COOKIE)

    def test_refresh_cookie_is_read_alongside(self):
        scope = _ws_scope(headers=[(
            b"cookie", b"stapel_jwt=acc; stapel_refresh_jwt=ref",
        )])
        assert ch._extract_credential(scope) == ("acc", "ref", ch.SOURCE_COOKIE)

    def test_refresh_cookie_alone_is_still_a_credential(self):
        # The access cookie expires first; refusing here is the 4401 that
        # taught the client the socket was permanently refused.
        scope = _ws_scope(headers=[(b"cookie", b"stapel_refresh_jwt=ref")])
        assert ch._extract_credential(scope) == (None, "ref", ch.SOURCE_COOKIE)

    @override_settings(JWT_COOKIE_NAME="custom_jwt")
    def test_cookie_name_follows_the_http_side(self):
        # Resolved through utils.jwt_cookie_names — the same call HTTP makes,
        # so the socket cannot read a cookie the HTTP side never sets.
        assert ch._extract_token(
            _ws_scope(headers=[(b"cookie", b"custom_jwt=tok")])
        ) == "tok"
        assert ch._extract_token(
            _ws_scope(headers=[(b"cookie", b"stapel_jwt=tok")])
        ) is None

    def test_unrelated_cookies_are_not_a_credential(self):
        scope = _ws_scope(headers=[(b"cookie", b"sessionid=xyz; theme=dark")])
        assert ch._extract_credential(scope) == (None, None, None)

    def test_malformed_cookie_header_does_not_raise(self):
        scope = _ws_scope(headers=[(b"cookie", b"=;;;garbage")])
        assert ch._extract_token(scope) is None

    # The cookie is LAST: an explicit credential always wins, and only a
    # browser with nothing else falls through to the ambient one.
    def test_explicit_channels_beat_the_cookie(self):
        for scope in (
            _ws_scope(headers=[(b"authorization", b"Bearer EXPLICIT"),
                               (b"cookie", b"stapel_jwt=COOKIE")]),
            _ws_scope(headers=[(b"cookie", b"stapel_jwt=COOKIE")],
                      subprotocols=["bearer", "EXPLICIT"]),
            _ws_scope(headers=[(b"cookie", b"stapel_jwt=COOKIE")],
                      query_string=b"token=EXPLICIT"),
        ):
            token, _, source = ch._extract_credential(scope)
            assert token == "EXPLICIT"
            assert source != ch.SOURCE_COOKIE


# ---------------------------------------------------------------------------
# _authenticate_token — mirrors the HTTP auth sequence
# ---------------------------------------------------------------------------

class TestAuthenticateToken:
    def _patch(self, monkeypatch, *, blacklisted=False, claims=None,
               user_blacklisted=False, user=object()):
        prov = type("P", (), {})()
        prov.is_blacklisted = lambda self=None, t=None: blacklisted
        prov.validate_token = lambda t, self=None: claims
        monkeypatch.setattr(
            "stapel_core.django.jwt.provider.jwt_provider", prov, raising=True
        )
        monkeypatch.setattr(
            "stapel_core.django.jwt.authentication.is_user_blacklisted",
            lambda uid: user_blacklisted,
            raising=True,
        )
        monkeypatch.setattr(
            "stapel_core.django.jwt.utils.get_or_create_user_from_jwt",
            lambda data: user,
            raising=True,
        )

    def test_valid(self, monkeypatch):
        sentinel_user = object()
        claims = {"user_id": "u1", "email": "u@x.com"}
        self._patch(monkeypatch, claims=claims, user=sentinel_user)
        user, out = ch._authenticate_token("tok")
        assert user is sentinel_user
        assert out == claims

    def test_token_blacklisted(self, monkeypatch):
        self._patch(monkeypatch, blacklisted=True, claims={"user_id": "u1"})
        assert ch._authenticate_token("tok") == (None, None)

    def test_invalid_token(self, monkeypatch):
        self._patch(monkeypatch, claims=None)
        assert ch._authenticate_token("tok") == (None, None)

    def test_user_blacklisted(self, monkeypatch):
        self._patch(
            monkeypatch, claims={"user_id": "u1"}, user_blacklisted=True
        )
        assert ch._authenticate_token("tok") == (None, None)

    def test_user_not_resolved(self, monkeypatch):
        self._patch(monkeypatch, claims={"user_id": "u1"}, user=None)
        assert ch._authenticate_token("tok") == (None, None)


# ---------------------------------------------------------------------------
# Middleware __call__ — scope population and rejection
# ---------------------------------------------------------------------------

class TestMiddlewareCall:
    def test_valid_populates_scope_and_calls_inner(self, monkeypatch):
        sentinel_user = object()
        claims = {"user_id": "u1", "email": "u@x.com"}
        monkeypatch.setattr(
            ch, "_authenticate_token", lambda t: (sentinel_user, claims)
        )
        inner = _RecordingInner()
        mw = ch.JWTAuthMiddleware(inner)
        send = _Sender()
        scope = _ws_scope(query_string=b"token=tok")

        _run(mw(scope, _connect_receiver(), send))

        assert inner.called
        assert inner.scope["user"] is sentinel_user
        assert inner.scope["stapel_claims"] == claims
        assert send.sent == [{"type": "websocket.accept"}]

    def test_invalid_token_closes_4401_before_accept(self, monkeypatch):
        monkeypatch.setattr(ch, "_authenticate_token", lambda t: (None, None))
        inner = _RecordingInner()
        mw = ch.JWTAuthMiddleware(inner)
        send = _Sender()

        _run(mw(_ws_scope(query_string=b"token=bad"), _connect_receiver(), send))

        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4401}]
        assert send.sent[0]["code"] == ch.CLOSE_CODE_UNAUTHORIZED

    def test_missing_token_closes_4401_without_calling_auth(self, monkeypatch):
        called = {"auth": False}

        def _auth(t):
            called["auth"] = True
            return (object(), {})

        monkeypatch.setattr(ch, "_authenticate_token", _auth)
        inner = _RecordingInner()
        mw = ch.JWTAuthMiddleware(inner)
        send = _Sender()

        _run(mw(_ws_scope(), _connect_receiver(), send))

        assert called["auth"] is False  # no token -> auth never attempted
        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4401}]

    def test_expired_token_rejected(self, monkeypatch):
        # Expired == validate_token returns None -> _authenticate_token (None,None)
        prov = type("P", (), {})()
        prov.is_blacklisted = lambda t: False
        prov.validate_token = lambda t: None
        monkeypatch.setattr(
            "stapel_core.django.jwt.provider.jwt_provider", prov, raising=True
        )
        inner = _RecordingInner()
        mw = ch.JWTAuthMiddleware(inner)
        send = _Sender()

        _run(mw(_ws_scope(query_string=b"token=expired"), _connect_receiver(), send))

        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4401}]

    def test_auth_exception_rejects_without_error_log(self, monkeypatch, caplog):
        def _boom(t):
            raise RuntimeError("db down")

        monkeypatch.setattr(ch, "_authenticate_token", _boom)
        inner = _RecordingInner()
        mw = ch.JWTAuthMiddleware(inner)
        send = _Sender()

        with caplog.at_level(logging.DEBUG, logger=ch.logger.name):
            _run(mw(_ws_scope(query_string=b"token=tok"), _connect_receiver(), send))

        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4401}]
        # Silent: nothing at WARNING or above, and no exception traceback logged.
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert all(r.exc_info is None for r in caplog.records)

    def test_non_websocket_scope_passes_through(self, monkeypatch):
        monkeypatch.setattr(
            ch, "_authenticate_token", lambda t: pytest.fail("auth ran on http")
        )
        seen = {}

        async def inner(scope, receive, send):
            seen["scope"] = scope

        mw = ch.JWTAuthMiddleware(inner)
        http_scope = {"type": "http"}
        _run(mw(http_scope, _connect_receiver(), _Sender()))

        assert seen["scope"] is http_scope  # untouched, no auth, no copy needed

    def test_deny_still_closes_when_receive_raises(self, monkeypatch):
        # Transport already gone: receive() raises -> we still send the close.
        monkeypatch.setattr(ch, "_authenticate_token", lambda t: (None, None))
        mw = ch.JWTAuthMiddleware(_RecordingInner())
        send = _Sender()

        async def receive():
            raise ConnectionError("gone")

        _run(mw(_ws_scope(query_string=b"token=bad"), receive, send))

        assert send.sent == [{"type": "websocket.close", "code": 4401}]

    def test_stack_factory_returns_middleware(self):
        inner = _RecordingInner()
        stack = ch.JWTAuthMiddlewareStack(inner)
        assert isinstance(stack, ch.JWTAuthMiddleware)
        assert stack.inner is inner


# ---------------------------------------------------------------------------
# TestBrowserCookieHandshake — the client that actually exists (0.44.1)
#
# Every test here sends what a browser sends and nothing a browser cannot:
# the JWT cookie in the handshake headers, an Origin header, and NO
# Authorization header. _browser_scope asserts the absence.
# ---------------------------------------------------------------------------

class TestBrowserCookieHandshake:
    @pytest.fixture(autouse=True)
    def _authenticating(self, monkeypatch):
        self.user = object()
        self.claims = {"user_id": "u1", "email": "u@x.com"}
        self.seen = []

        def _auth(token):
            self.seen.append(token)
            return (self.user, self.claims)

        monkeypatch.setattr(ch, "_authenticate_token", _auth)

    def _run_handshake(self, scope, allowed_origins=None):
        inner = _RecordingInner()
        # Constructed the way a host constructs it — one argument — so these
        # tests fail on an older core with the production SYMPTOM (handshake
        # closed) rather than with a TypeError about a new keyword.
        mw = (
            ch.JWTAuthMiddleware(inner)
            if allowed_origins is None
            else ch.JWTAuthMiddleware(inner, allowed_origins=allowed_origins)
        )
        send = _Sender()
        _run(mw(scope, _connect_receiver(), send))
        return inner, send

    # ---- accept ----------------------------------------------------------

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[ALLOWED])
    def test_cookie_from_an_allowed_origin_is_accepted(self):
        """FAILS ON 0.43.0: no cookie branch, so this closes 4401.

        This is the whole incident in one assertion — the handshake a real
        browser makes, admitted.
        """
        inner, send = self._run_handshake(_browser_scope())

        assert inner.called
        assert inner.scope["user"] is self.user
        assert inner.scope["stapel_claims"] == self.claims
        assert inner.scope["stapel_auth_source"] == ch.SOURCE_COOKIE
        assert send.sent == [{"type": "websocket.accept"}]
        assert self.seen == ["cookie.tok.en"]

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=["http://localhost:5173"])
    def test_origin_matching_ignores_case_and_default_ports(self):
        inner, _ = self._run_handshake(
            _browser_scope(origin="HTTP://LocalHost:5173")
        )
        assert inner.called

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[], STAPEL_REALTIME={
        "ALLOWED_ORIGINS": [ALLOWED],
    })
    def test_realtime_allowlist_is_honoured_so_it_is_declared_once(self):
        """A host already running stapel-realtime must not declare twice —
        two lists that can disagree is how two layers give contradictory
        verdicts about the same socket."""
        inner, _ = self._run_handshake(_browser_scope())
        assert inner.called

    # ---- refuse ----------------------------------------------------------

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[ALLOWED])
    def test_cross_origin_cookie_handshake_is_refused(self):
        """Cross-Site WebSocket Hijacking, refused.

        The attacker's page cannot read the cookie, but the browser attaches
        it anyway and no same-origin policy or CORS preflight stands in the
        way. Only the Origin check does.
        """
        inner, send = self._run_handshake(_browser_scope(origin=EVIL))

        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4403}]
        assert send.sent[0]["code"] == ch.CLOSE_CODE_FORBIDDEN

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[ALLOWED])
    def test_refusal_happens_before_the_token_is_ever_validated(self):
        """An unlisted origin must not even learn whether the victim's cookie
        is currently valid."""
        inner, send = self._run_handshake(_browser_scope(origin=EVIL))
        assert self.seen == []
        assert not inner.called

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[])
    def test_empty_allowlist_fails_closed(self):
        """An empty allowlist is a misconfiguration, NOT a wildcard."""
        inner, send = self._run_handshake(_browser_scope())
        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4403}]

    def test_no_allowlist_setting_at_all_fails_closed(self):
        with override_settings():
            from django.conf import settings
            for name in ("STAPEL_WS_ALLOWED_ORIGINS", "STAPEL_REALTIME"):
                if hasattr(settings, name):
                    delattr(settings._wrapped, name)
            inner, send = self._run_handshake(_browser_scope())
        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4403}]

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[ALLOWED])
    def test_cookie_handshake_without_origin_is_refused(self):
        """Browsers always send Origin on a handshake. A client that does not
        is one that could have sent a non-ambient credential instead."""
        inner, send = self._run_handshake(_browser_scope(origin=None))
        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4403}]

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=["app.example.com"])
    def test_malformed_allowlist_entry_refuses_rather_than_falls_open(self):
        """A typo must not be the thing that decides there is no guard."""
        inner, send = self._run_handshake(_browser_scope())
        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4403}]

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[ALLOWED])
    def test_invalid_cookie_token_still_closes_4401_not_4403(self, monkeypatch):
        """The origin was fine; the credential was not. The two refusals stay
        distinguishable so an operator can tell them apart in a log."""
        monkeypatch.setattr(ch, "_authenticate_token", lambda t: (None, None))
        inner, send = self._run_handshake(_browser_scope())
        assert not inner.called
        assert send.sent == [{"type": "websocket.close", "code": 4401}]

    @override_settings(STAPEL_WS_ALLOWED_ORIGINS=[])
    def test_unguarded_refusal_warns_once_not_per_handshake(self, caplog):
        inner = _RecordingInner()
        mw = ch.JWTAuthMiddleware(inner)
        with caplog.at_level(logging.WARNING, logger=ch.logger.name):
            for _ in range(5):
                _run(mw(_browser_scope(), _connect_receiver(), _Sender()))
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "stapel_core.jwt.E001" in warnings[0].getMessage()

    # ---- the non-ambient channels are deliberately NOT gated -------------

    def test_subprotocol_token_is_not_gated_by_origin(self):
        """A subprotocol token is not ambient: an attacker's page cannot
        produce one it has never seen. Gating it would refuse every
        service-to-service and native client, which send no Origin at all."""
        inner, send = self._run_handshake(
            _ws_scope(subprotocols=["bearer", "tok"],
                      headers=[(b"origin", EVIL.encode())])
        )
        assert inner.called
        assert inner.scope["stapel_auth_source"] == ch.SOURCE_SUBPROTOCOL

    def test_header_and_query_tokens_are_not_gated_by_origin(self):
        for scope, source in (
            (_ws_scope(headers=[(b"authorization", b"Bearer tok"),
                                (b"origin", EVIL.encode())]),
             ch.SOURCE_HEADER),
            (_ws_scope(query_string=b"token=tok",
                       headers=[(b"origin", EVIL.encode())]),
             ch.SOURCE_QUERY),
        ):
            inner, _ = self._run_handshake(scope)
            assert inner.called
            assert inner.scope["stapel_auth_source"] == source


class TestCookieRefreshOnHandshake:
    """An access cookie lasts an hour; the refresh cookie behind it lasts
    days. A tab left open past expiry reconnects holding both — and a 4401
    there is the close that taught the client to stop retrying."""

    def _provider(self, monkeypatch, new_token):
        prov = type("P", (), {})()
        prov.refresh_access_token = lambda rt, loader: new_token
        monkeypatch.setattr(
            "stapel_core.django.jwt.provider.jwt_provider", prov, raising=True
        )

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_expired_access_cookie_falls_through_to_the_refresh_cookie(
        self, monkeypatch
    ):
        user, claims = object(), {"user_id": "u1"}
        self._provider(monkeypatch, "fresh.access.token")
        monkeypatch.setattr(
            ch, "_authenticate_token",
            lambda t: (user, claims) if t == "fresh.access.token" else (None, None),
        )
        out = ch._authenticate_cookie("expired.access", "live.refresh")
        assert out == (user, claims, "fresh.access.token")

    @override_settings(JWT_REFRESH_ALLOWED=False)
    def test_refresh_is_gated_on_the_same_flag_http_reads(self, monkeypatch):
        self._provider(monkeypatch, "fresh.access.token")
        monkeypatch.setattr(ch, "_authenticate_token", lambda t: (None, None))
        assert ch._authenticate_cookie("expired", "refresh") == (None, None, None)

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_refused_refresh_is_not_authenticated(self, monkeypatch):
        self._provider(monkeypatch, None)  # tombstoned/deleted/inactive uid
        monkeypatch.setattr(ch, "_authenticate_token", lambda t: (None, None))
        assert ch._authenticate_cookie("expired", "refresh") == (None, None, None)

    @override_settings(JWT_REFRESH_ALLOWED=True)
    def test_valid_access_cookie_never_reaches_the_refresh_path(self, monkeypatch):
        user, claims = object(), {"user_id": "u1"}
        def _boom(rt, loader):
            pytest.fail("refresh attempted with a valid access cookie")
        prov = type("P", (), {})()
        prov.refresh_access_token = _boom
        monkeypatch.setattr(
            "stapel_core.django.jwt.provider.jwt_provider", prov, raising=True
        )
        monkeypatch.setattr(ch, "_authenticate_token", lambda t: (user, claims))
        assert ch._authenticate_cookie("good", "refresh") == (user, claims, None)

    @override_settings(JWT_REFRESH_ALLOWED=True, STAPEL_WS_ALLOWED_ORIGINS=[ALLOWED])
    def test_refreshed_token_is_stamped_into_the_scope(self, monkeypatch):
        """A handshake has no response to set a cookie on, so the host is the
        only thing that can hand the fresh token back."""
        user, claims = object(), {"user_id": "u1"}
        self._provider(monkeypatch, "fresh.access.token")
        monkeypatch.setattr(
            ch, "_authenticate_token",
            lambda t: (user, claims) if t == "fresh.access.token" else (None, None),
        )
        inner = _RecordingInner()
        mw = ch.JWTAuthMiddleware(inner)
        scope = _browser_scope(
            cookie="stapel_jwt=expired; stapel_refresh_jwt=live"
        )
        _run(mw(scope, _connect_receiver(), _Sender()))

        assert inner.called
        assert inner.scope["stapel_refreshed_access_token"] == "fresh.access.token"


# ---------------------------------------------------------------------------
# Optional-dependency contract
# ---------------------------------------------------------------------------

class TestOptionalDependency:
    def test_not_imported_on_normal_django_start(self):
        """Importing the HTTP JWT stack must not drag in the channels submodule."""
        code = (
            "import sys\n"
            "from stapel_core.testing import configure_django\n"
            "configure_django(installed_apps=[])\n"
            "import stapel_core.django\n"
            "import stapel_core.django.jwt.authentication\n"
            "import stapel_core.django.jwt.middleware\n"
            "import stapel_core.django.jwt.provider\n"
            "assert 'stapel_core.django.jwt.channels' not in sys.modules, "
            "'channels submodule imported on normal start'\n"
            "print('OK')\n"
        )
        # Run from a neutral cwd so the repo-root `django/` dir cannot shadow
        # the real Django package (see tests/conftest.py).
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd="/",
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_import_without_channels_raises_clear_error(self):
        """Absent `channels`, importing the submodule gives a helpful ImportError."""
        saved = {
            k: v for k, v in sys.modules.items()
            if k == "channels" or k.startswith("channels.")
        }
        saved_submod = sys.modules.pop("stapel_core.django.jwt.channels", None)
        try:
            # Poison the channels imports so `from channels.db import ...` fails.
            for name in list(saved) + ["channels", "channels.db"]:
                sys.modules[name] = None
            with pytest.raises(ImportError, match=r"stapel-core\[channels\]"):
                importlib.import_module("stapel_core.django.jwt.channels")
        finally:
            for name in ["channels", "channels.db"] + list(saved):
                sys.modules.pop(name, None)
            sys.modules.update(saved)
            sys.modules.pop("stapel_core.django.jwt.channels", None)
            if saved_submod is not None:
                # Restore the freshly re-imported module for any later tests.
                importlib.import_module("stapel_core.django.jwt.channels")
