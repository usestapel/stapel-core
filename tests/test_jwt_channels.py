"""Tests for stapel_core.django.jwt.channels — Channels JWT auth middleware.

Covers: token extraction from all three channels (Authorization header,
Sec-WebSocket-Protocol subprotocol in both shapes, ?token= query param) and
their precedence; the full authenticate/reject flow (valid / expired / missing
/ blacklisted token, banned user); rejection with close code 4401 before
accept; silent (no-error-log) rejection on exceptions; non-websocket
pass-through; and the optional-dependency contract (submodule is not imported
on a normal Django start, and importing it without `channels` raises a clear
ImportError).
"""

import asyncio
import importlib
import logging
import subprocess
import sys

import pytest

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
