"""Adversarial tests for the fleet's shared SSRF-hardened fetcher.

The network is faked at two seams so no test ever egresses:
  * ``socket.getaddrinfo`` — decides what a hostname resolves to;
  * ``stapel_core.net.safe_fetch._open`` — returns a canned HTTP response
    instead of a real pinned TLS connection, so the redirect, deadline and
    streaming logic is exercised for real.
"""
import io
import socket
import time

import pytest

from stapel_core.net import safe_fetch
from stapel_core.net.safe_fetch import SafeFetchError, fetch_bytes


def _addrinfo(ip: str, port: int = 443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._buf = io.BytesIO(body)
        self.closed = False

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        self.closed = True


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, **kw: _addrinfo("93.184.216.34", port)
    )


def _serve(monkeypatch, *responses):
    """Queue one FakeResponse per hop and record the IPs actually connected to."""
    seen = []
    queue = list(responses)

    def fake_open(host, ip, port, path, **kwargs):
        seen.append((host, str(ip), path))
        return queue.pop(0)

    monkeypatch.setattr(safe_fetch, "_open", fake_open)
    return seen


# --------------------------------------------------------------------------- #
# Scheme and host
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x.bin",
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/x",
    ],
)
def test_only_https_is_accepted(url):
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes(url, max_bytes=1024)
    assert exc.value.code == "scheme_not_https"


def test_url_without_host_is_refused():
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https:///nowhere", max_bytes=1024)
    assert exc.value.code == "no_host"


def test_host_allowlist_is_enforced(public_dns, monkeypatch):
    _serve(monkeypatch, FakeResponse(body=b"ok"))
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes(
            "https://evil.example/x", max_bytes=1024, allowed_hosts=["api.figma.com"]
        )
    assert exc.value.code == "host_not_allowed"


# --------------------------------------------------------------------------- #
# Address validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",            # loopback
        "10.0.0.5",             # RFC1918
        "192.168.1.1",          # RFC1918
        "169.254.169.254",      # cloud metadata
        "100.64.0.1",           # CGNAT
        "0.0.0.0",              # unspecified
        "::1",                  # v6 loopback
        "fd00::1",              # ULA
        "::ffff:169.254.169.254",   # v4-mapped metadata
        "64:ff9b::a9fe:a9fe",       # NAT64-encoded metadata
        "2002:a9fe:a9fe::1",        # 6to4-encoded metadata
    ],
)
def test_non_public_addresses_are_blocked(monkeypatch, ip):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, **kw: _addrinfo(ip, port)
    )
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://internal.example/x", max_bytes=1024)
    assert exc.value.code == "blocked_ip"


def test_a_single_forbidden_answer_poisons_the_whole_name(monkeypatch):
    """A name answering public+private is hostile, not 'pick the good one'."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kw: _addrinfo("93.184.216.34", port)
        + _addrinfo("169.254.169.254", port),
    )
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://mixed.example/x", max_bytes=1024)
    assert exc.value.code == "blocked_ip"


def test_dns_failure_is_structured(monkeypatch):
    def boom(*a, **kw):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://nx.example/x", max_bytes=1024)
    assert exc.value.code == "dns_resolution_failed"


def test_connection_is_pinned_to_the_validated_ip(public_dns, monkeypatch):
    seen = _serve(monkeypatch, FakeResponse(body=b"ok"))
    fetch_bytes("https://example.com/x", max_bytes=1024)
    assert seen == [("example.com", "93.184.216.34", "/x")]


# --------------------------------------------------------------------------- #
# Redirects
# --------------------------------------------------------------------------- #
def test_redirect_target_is_revalidated(monkeypatch):
    """The classic bypass: public first hop, private Location."""
    resolutions = iter(["93.184.216.34", "169.254.169.254"])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kw: _addrinfo(next(resolutions), port),
    )
    _serve(
        monkeypatch,
        FakeResponse(302, {"Location": "https://metadata.example/latest"}),
    )
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://example.com/x", max_bytes=1024)
    assert exc.value.code == "blocked_ip"


def test_redirect_to_non_https_is_refused(public_dns, monkeypatch):
    _serve(monkeypatch, FakeResponse(302, {"Location": "http://example.com/y"}))
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://example.com/x", max_bytes=1024)
    assert exc.value.code == "scheme_not_https"


def test_redirect_hops_are_capped(public_dns, monkeypatch):
    _serve(
        monkeypatch,
        *[FakeResponse(302, {"Location": "https://example.com/next"}) for _ in range(5)],
    )
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://example.com/x", max_bytes=1024, max_redirects=2)
    assert exc.value.code == "too_many_redirects"


def test_redirect_without_location_is_refused(public_dns, monkeypatch):
    _serve(monkeypatch, FakeResponse(302, {}))
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://example.com/x", max_bytes=1024)
    assert exc.value.code == "redirect_no_location"


def test_followed_redirect_reports_the_final_url(public_dns, monkeypatch):
    _serve(
        monkeypatch,
        FakeResponse(301, {"Location": "/moved"}),
        FakeResponse(200, {"Content-Type": "text/plain"}, b"hi"),
    )
    result = fetch_bytes("https://example.com/x", max_bytes=1024)
    assert result.final_url == "https://example.com/moved"
    assert result.content == b"hi"


# --------------------------------------------------------------------------- #
# Size, deadline, status, content type
# --------------------------------------------------------------------------- #
def test_oversize_body_aborts_mid_stream(public_dns, monkeypatch):
    _serve(monkeypatch, FakeResponse(body=b"A" * 500_000))
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://example.com/x", max_bytes=1024)
    assert exc.value.code == "too_large"


def test_lying_content_length_does_not_help(public_dns, monkeypatch):
    """Declared small, delivers large — the streaming cap still catches it."""
    _serve(
        monkeypatch,
        FakeResponse(headers={"Content-Length": "10"}, body=b"A" * 500_000),
    )
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://example.com/x", max_bytes=1024)
    assert exc.value.code == "too_large"


def test_oversize_content_length_is_refused_before_reading(public_dns, monkeypatch):
    _serve(
        monkeypatch,
        FakeResponse(headers={"Content-Length": "999999"}, body=b"A"),
    )
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://example.com/x", max_bytes=1024)
    assert exc.value.code == "too_large"


def test_total_deadline_bounds_the_whole_operation(public_dns, monkeypatch):
    """A slow-drip server must not outlive the budget one socket timeout at a time."""
    clock = iter([0.0, 0.0, 5.0, 10.0, 20.0, 40.0, 80.0, 160.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    _serve(
        monkeypatch,
        *[FakeResponse(302, {"Location": "https://example.com/next"}) for _ in range(6)],
    )
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes(
            "https://example.com/x",
            max_bytes=1024,
            total_deadline=15.0,
            max_redirects=10,
        )
    assert exc.value.code == "deadline_exceeded"


def test_non_200_is_refused(public_dns, monkeypatch):
    _serve(monkeypatch, FakeResponse(500, body=b"boom"))
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes("https://example.com/x", max_bytes=1024)
    assert exc.value.code == "bad_status"


def test_content_type_allowlist(public_dns, monkeypatch):
    _serve(monkeypatch, FakeResponse(headers={"Content-Type": "text/html"}, body=b"<p>"))
    with pytest.raises(SafeFetchError) as exc:
        fetch_bytes(
            "https://example.com/x", max_bytes=1024, allowed_content_types=("audio/",)
        )
    assert exc.value.code == "content_type_not_allowed"


def test_allowed_content_type_passes(public_dns, monkeypatch):
    _serve(
        monkeypatch,
        FakeResponse(headers={"Content-Type": "audio/mpeg; charset=x"}, body=b"ID3"),
    )
    result = fetch_bytes(
        "https://example.com/a.mp3", max_bytes=1024, allowed_content_types=("audio/",)
    )
    assert result.content == b"ID3"
    assert result.content_type == "audio/mpeg"


def test_max_bytes_is_mandatory_and_positive():
    """A caller that has not decided how much memory a stranger may fill."""
    with pytest.raises(TypeError):
        fetch_bytes("https://example.com/x")
    with pytest.raises(ValueError):
        fetch_bytes("https://example.com/x", max_bytes=0)


def test_open_pins_the_socket_and_sends_no_credential(monkeypatch):
    """Exercises the real ``_open`` — the thing that defeats DNS rebinding.

    Only the primitives underneath it are faked, so both the conn.sock
    pinning and the exact header set are observed rather than assumed.
    """
    import http.client
    import ipaddress
    import ssl

    connected = {}
    sent = {}

    class _RawSocket:
        def close(self):
            pass

    class _Context:
        def wrap_socket(self, raw, server_hostname=None):
            connected["sni"] = server_hostname
            return raw

    monkeypatch.setattr(
        safe_fetch.socket,
        "create_connection",
        lambda address, timeout=None: connected.setdefault("address", address)
        or _RawSocket(),
    )
    monkeypatch.setattr(ssl, "create_default_context", lambda: _Context())
    monkeypatch.setattr(
        http.client.HTTPSConnection,
        "request",
        lambda self, method, url, headers=None, **kw: sent.update(
            {"method": method, "url": url, "headers": headers}
        ),
    )
    monkeypatch.setattr(
        http.client.HTTPSConnection, "getresponse", lambda self: FakeResponse(body=b"")
    )

    safe_fetch._open(
        "example.com",
        ipaddress.ip_address("93.184.216.34"),
        443,
        "/x",
        timeout=5.0,
        accept="*/*",
        user_agent="stapel-safe-fetch/1.0",
    )

    # TCP went to the validated literal, TLS still validates the real name.
    assert connected["address"] == ("93.184.216.34", 443)
    assert connected["sni"] == "example.com"
    assert {k.lower() for k in sent["headers"]} == {"host", "user-agent", "accept"}


def test_response_is_closed_on_failure(public_dns, monkeypatch):
    response = FakeResponse(500, body=b"boom")
    monkeypatch.setattr(safe_fetch, "_open", lambda *a, **kw: response)
    with pytest.raises(SafeFetchError):
        fetch_bytes("https://example.com/x", max_bytes=1024)
    assert response.closed is True
