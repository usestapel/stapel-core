"""The sweep behind the 0.44.x WebSocket fix — one door, one cookie name.

0.44.0 shipped the cookie as the fourth credential channel on the Channels
handshake, because a browser cannot set an ``Authorization`` header on
``new WebSocket()``. The per-case behaviour is proved in
``test_jwt_channels.py`` and ``test_jwt_ws_origin.py``. What those files cannot
prove is the *absence* of a second door, and the absence is the half that let
the original defect live for months: the socket was built, mounted, proxied and
smoke-tested against a client that does not exist.

So this file sweeps rather than exercises:

1. Every credential channel the extractor can report is classified as ambient
   or not, so a fifth channel cannot be added without deciding whether the
   origin gate applies to it.
2. The handshake reads the cookie NAME that the HTTP side sets — under a
   renamed setting, not just the default.
3. Nothing else in the package resolves that name from settings on its own,
   and nothing else in the package reads a credential off an ASGI scope.
"""
import re
from pathlib import Path

import pytest
from django.test import override_settings

import stapel_core
from stapel_core.django.jwt import utils as jwt_utils

ch = pytest.importorskip("stapel_core.django.jwt.channels")

PACKAGE_ROOT = Path(stapel_core.__file__).resolve().parent

#: Directories that are not the shipped package.
_SKIP_PREFIXES = ("tests", "build", "dist", ".venv", "docs", "stapel_core.egg-info")


def _package_sources():
    """Every shipped ``.py`` file of stapel-core, as (relative path, text)."""
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel = path.relative_to(PACKAGE_ROOT)
        if rel.parts[0] in _SKIP_PREFIXES or "__pycache__" in rel.parts:
            continue
        yield rel, path.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# 1. Every channel is classified, so a fifth one cannot skip the decision
# ---------------------------------------------------------------------------

TOKEN = "tok.en.value"


def _scope(headers=(), subprotocols=None, query_string=b""):
    scope = {"type": "websocket", "headers": list(headers), "query_string": query_string}
    if subprotocols is not None:
        scope["subprotocols"] = list(subprotocols)
    return scope


#: One scope per channel, each holding ONLY that channel's credential.
CHANNEL_SCOPES = {
    ch.SOURCE_HEADER: _scope(headers=[(b"authorization", b"Bearer " + TOKEN.encode())]),
    ch.SOURCE_SUBPROTOCOL: _scope(subprotocols=["bearer", TOKEN]),
    ch.SOURCE_QUERY: _scope(query_string=b"token=" + TOKEN.encode()),
    ch.SOURCE_COOKIE: _scope(headers=[(b"cookie", b"stapel_jwt=" + TOKEN.encode())]),
}


@pytest.mark.parametrize("source,scope", sorted(CHANNEL_SCOPES.items()))
def test_each_declared_channel_is_the_one_the_extractor_reports(source, scope):
    token, _refresh, reported = ch._extract_credential(scope)
    assert token == TOKEN
    assert reported == source


def test_every_channel_the_extractor_can_report_is_classified():
    """A fifth channel must declare whether it is ambient authority.

    ``AMBIENT_SOURCES`` is what the middleware gates on ``Origin``. A channel
    added to the extractor and forgotten here would be admitted cross-site
    without an origin check if it is ambient, or gated needlessly if it is
    not — either way the decision must be made, not defaulted.
    """
    declared = {
        value
        for name, value in vars(ch).items()
        if name.startswith("SOURCE_") and isinstance(value, str)
    }
    assert declared == set(CHANNEL_SCOPES)
    # The cookie is the only channel the browser attaches without the page
    # asking for it; everything else requires the page to already hold a token.
    assert ch.AMBIENT_SOURCES == {ch.SOURCE_COOKIE}
    assert ch.AMBIENT_SOURCES <= declared


def test_a_scope_with_nothing_a_browser_can_send_yields_no_credential():
    assert ch._extract_credential(_scope()) == (None, None, None)


# ---------------------------------------------------------------------------
# 2. The socket reads the cookie the HTTP side sets — under a RENAMED setting
# ---------------------------------------------------------------------------

@override_settings(
    JWT_COOKIE_NAME="renamed_access",
    JWT_REFRESH_COOKIE_NAME="renamed_refresh",
)
def test_the_handshake_follows_a_renamed_cookie():
    """The default name proves nothing: both halves hardcoded the same literal.

    Only a renamed deployment separates "resolves the name" from "happens to
    agree with the default".
    """
    assert jwt_utils.jwt_cookie_names() == ("renamed_access", "renamed_refresh")

    renamed = _scope(headers=[(b"cookie", b"renamed_access=A; renamed_refresh=R")])
    assert ch._token_from_cookies(renamed) == ("A", "R")

    # The old default is no longer a credential anywhere.
    stale = _scope(headers=[(b"cookie", b"stapel_jwt=A; stapel_refresh_jwt=R")])
    assert ch._token_from_cookies(stale) == (None, None)
    assert ch._extract_credential(stale) == (None, None, None)


@override_settings(JWT_COOKIE_NAME="renamed_access")
def test_the_http_extractor_and_the_handshake_agree_on_the_name():
    """The two halves of the same deployment, asked the same question."""

    class _Request:
        COOKIES = {"renamed_access": "A"}
        META = {}
        headers = {}

    http_access, _ = jwt_utils.extract_jwt_from_request(_Request())
    ws_access, _ = ch._token_from_cookies(
        _scope(headers=[(b"cookie", b"renamed_access=A")])
    )
    assert http_access == ws_access == "A"


# ---------------------------------------------------------------------------
# 3. Nothing else re-derives the name, and nothing else reads an ASGI scope
# ---------------------------------------------------------------------------

_GETATTR_COOKIE_NAME = re.compile(
    r"getattr\(\s*settings\s*,\s*['\"]JWT_(?:REFRESH_)?COOKIE_NAME['\"]"
)

#: The ONE resolution lives here; everything else calls it.
_COOKIE_NAME_RESOLVERS = {Path("django/jwt/utils.py")}


def test_only_one_module_resolves_the_jwt_cookie_name():
    """Five copies of the default is how one half sets a cookie the other
    half never reads — which is the shape the socket defect had."""
    offenders = sorted(
        str(rel)
        for rel, text in _package_sources()
        if _GETATTR_COOKIE_NAME.search(text) and rel not in _COOKIE_NAME_RESOLVERS
    )
    assert offenders == [], (
        "re-derives the JWT cookie name instead of calling "
        "stapel_core.django.jwt.utils.jwt_cookie_names(): " + ", ".join(offenders)
    )


@override_settings(JWT_COOKIE_NAME="renamed_access")
def test_the_published_openapi_scheme_names_the_deployment_cookie():
    """The schema is a contract with the client. A service that renamed the
    cookie was publishing an auth scheme naming a cookie it never issues —
    the same "one half sets, the other half reads" split, in the docs."""
    pytest.importorskip("drf_spectacular")
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    extension = _register_jwt_auth_extension()
    definition = extension.get_security_definition(extension, None)
    assert definition["in"] == "cookie"
    assert definition["name"] == "renamed_access"


_ASGI_HEADER_READ = re.compile(r"scope(?:\.get\(|\[)\s*['\"]headers['\"]")

#: The single WebSocket credential door.
_ASGI_HEADER_READERS = {Path("django/jwt/channels.py")}


def test_the_websocket_handshake_has_exactly_one_credential_door():
    """A second reader of the handshake headers is a second authentication
    path — and the one that would not inherit the origin gate."""
    offenders = sorted(
        str(rel)
        for rel, text in _package_sources()
        if _ASGI_HEADER_READ.search(text) and rel not in _ASGI_HEADER_READERS
    )
    assert offenders == [], (
        "reads credentials off an ASGI scope outside the JWT handshake "
        "middleware: " + ", ".join(offenders)
    )
