"""SSRF-hardened outbound HTTP fetch — the fleet's only safe way to pull a URL.

Why this lives in core
----------------------
The hardening below was written once, in ``stapel-cdn``'s import-from-URL
path, and it is good: https-only, every resolved address validated, the
connection pinned to a validated IP, every redirect re-validated from
scratch, a byte cap enforced mid-stream. Then the 2026-08-11 audit found
``stapel_agent.stt`` handing an arbitrary caller-supplied URL straight to
``requests.get(..., timeout=600)`` and materialising ``resp.content`` — no
scheme check, no IP check, no cap (AGENT-01). Same fleet, same threat, and
the second consumer never picked the mechanism up, because the mechanism sat
inside a module nobody else depends on.

So it moves here. Any module that fetches a URL a caller can influence uses
:func:`fetch_bytes`; nothing hand-rolls ``requests.get`` on an untrusted URL
again. ``stapel_tools``' SEC lint enforces that across the fleet.

What every hop is guarded against
---------------------------------
* **https-only** — checked up front and again on every redirect target.
* **DNS → IP allowlist** — the host is resolved once and *all* answers are
  validated against the forbidden ranges (RFC1918/ULA private, loopback,
  link-local including the 169.254.169.254 cloud-metadata endpoint,
  multicast, reserved, unspecified, CGNAT 100.64.0.0/10). IPv4-mapped, 6to4
  and NAT64 (``64:ff9b::/96``) IPv6 encodings are unwrapped to the embedded
  IPv4 first, so a metadata address cannot be smuggled in re-encoded. If
  *any* answer is forbidden the fetch is refused — a name answering with a
  mix of public and private records is hostile, not "pick the good one".
* **anti-DNS-rebinding** — resolve, validate, then connect to *that exact
  IP* while presenting the real hostname for SNI/certificate validation and
  the ``Host`` header. There is no second lookup between check and connect.
* **redirects are never followed by the client library** — the loop is
  driven here so each ``Location`` gets the full scheme+DNS+IP treatment
  before anything connects to it, and the hop count is capped.
* **no credential forwarding** — this function never sends an ``Authorization``
  header, so there is nothing to leak across an origin change. Callers that
  need one are fetching a trusted origin and should say so with
  ``allowed_hosts``.
* **byte cap enforced while streaming** — the transfer aborts the instant it
  crosses the cap, before an oversized body is buffered whole. A lying
  ``Content-Length`` gains nothing.
* **total deadline** — a per-socket timeout alone bounds nothing: a server
  that trickles one byte per timeout window holds a worker forever. The
  deadline covers the whole operation including every redirect hop.
* **content-type allowlist** — optional, checked before the body is read.

Failures raise :class:`SafeFetchError` with a stable machine-readable
``.code``. There is no path that returns a body for an address that was not
validated: this never fails open.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urljoin, urlsplit

_CHUNK = 64 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")

#: Default ceiling on the whole operation. Deliberately small: a fetch on a
#: request or worker path that has not finished in half a minute is a
#: resource leak, not a slow success.
DEFAULT_TOTAL_DEADLINE = 30.0
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_REDIRECTS = 3


class SafeFetchError(Exception):
    """Structured failure of a guarded fetch.

    ``code`` is a stable machine token (``scheme_not_https``, ``blocked_ip``,
    ``too_large``, ``deadline_exceeded``, ...) suitable for logging and
    metrics. Callers on a security path must treat it as fatal — it is never
    converted into a fail-open default.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class SafeFetchResult:
    """A body that passed every guard, plus what it was served as."""

    content: bytes
    content_type: str
    final_url: str


def _nat64_embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Extract the IPv4 address embedded in a NAT64 (RFC 6052) address.

    Only the well-known ``64:ff9b::/96`` prefix is unwrapped — the one a
    NAT64/DNS64 resolver synthesizes AAAA records under. ``ipv4_mapped`` and
    ``sixtofour`` do not know this prefix exists, so ``64:ff9b::a9fe:a9fe``
    (the metadata address) reads as an ordinary global IPv6 address to
    ``is_global`` unless it is unwrapped here first.
    """
    if ip in _NAT64_WELL_KNOWN_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def ip_is_forbidden(ip: ipaddress._BaseAddress) -> bool:
    """True for anything that is not a normal, routable public address.

    The individual flags are spelled out rather than left to ``is_global``
    because ``is_global`` semantics have shifted across CPython versions and
    because an auditor should be able to read the list. CGNAT (RFC 6598) is
    checked explicitly for the same reason.
    """
    if ip.version == 6:
        mapped = (
            getattr(ip, "ipv4_mapped", None)
            or getattr(ip, "sixtofour", None)
            or _nat64_embedded_ipv4(ip)
        )
        if mapped is not None:
            ip = mapped

    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 4 and ip in _CGNAT_RANGE)
    )


def _resolve_validated(host: str, port: int) -> ipaddress._BaseAddress:
    """Resolve *host*, validate every answer, return the address to pin to."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SafeFetchError(
            "dns_resolution_failed", f"cannot resolve {host!r}: {exc}"
        ) from exc

    if not infos:
        raise SafeFetchError("dns_resolution_failed", f"no addresses for {host!r}")

    first: ipaddress._BaseAddress | None = None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError as exc:  # pragma: no cover - getaddrinfo returns literals
            raise SafeFetchError("dns_resolution_failed", str(exc)) from exc
        if ip_is_forbidden(ip):
            raise SafeFetchError(
                "blocked_ip", f"{host!r} resolves to non-public address {ip}"
            )
        if first is None:
            first = ip
    assert first is not None
    return first


def _open(
    host: str,
    ip: ipaddress._BaseAddress,
    port: int,
    path: str,
    *,
    timeout: float,
    accept: str,
    user_agent: str,
) -> http.client.HTTPResponse:
    """Open one HTTPS request to the already-validated *ip*.

    TCP goes to ``ip`` with no second name lookup; TLS SNI, certificate
    validation and the ``Host`` header still use the real hostname, so a
    valid public certificate is required and rebinding buys nothing. Kept as
    a seam so tests can substitute the network.
    """
    raw = socket.create_connection((str(ip), port), timeout=timeout)
    try:
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise

    conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    conn.sock = sock
    conn.request(
        "GET",
        path or "/",
        headers={"Host": host, "User-Agent": user_agent, "Accept": accept},
    )
    return conn.getresponse()


def fetch_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = DEFAULT_TIMEOUT,
    total_deadline: float = DEFAULT_TOTAL_DEADLINE,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    accept: str = "*/*",
    allowed_content_types: Sequence[str] | None = None,
    allowed_hosts: Sequence[str] | None = None,
    user_agent: str = "stapel-safe-fetch/1.0",
) -> SafeFetchResult:
    """Fetch *url* under the full guard set. See the module docstring.

    ``max_bytes`` is mandatory and has no default on purpose: a caller that
    has not decided how much of its memory a stranger may fill has not
    finished thinking about the fetch.

    ``allowed_content_types`` are matched as prefixes (``("image/",)``).
    ``allowed_hosts`` is an exact-match host allowlist applied to every hop —
    the right control when the remote is a known API rather than a
    user-supplied address.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    started = time.monotonic()
    allowed_hosts_set = {h.lower() for h in allowed_hosts} if allowed_hosts else None

    def _remaining() -> float:
        left = total_deadline - (time.monotonic() - started)
        if left <= 0:
            raise SafeFetchError(
                "deadline_exceeded", f"exceeded {total_deadline}s total deadline"
            )
        return left

    current = url
    hops = 0

    while True:
        parsed = urlsplit(current)
        if parsed.scheme != "https":
            raise SafeFetchError(
                "scheme_not_https", f"refusing non-https scheme {parsed.scheme!r}"
            )
        host = parsed.hostname
        if not host:
            raise SafeFetchError("no_host", "url has no host")
        if allowed_hosts_set is not None and host.lower() not in allowed_hosts_set:
            raise SafeFetchError("host_not_allowed", f"host {host!r} is not allowlisted")
        port = parsed.port or 443

        ip = _resolve_validated(host, port)

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        # Per-socket timeout never exceeds what is left of the total budget,
        # so a slow-drip server cannot outlive the deadline one read at a time.
        resp = _open(
            host,
            ip,
            port,
            path,
            timeout=min(timeout, _remaining()),
            accept=accept,
            user_agent=user_agent,
        )
        try:
            status = resp.status

            if status in _REDIRECT_STATUSES:
                location = resp.getheader("Location")
                if not location:
                    raise SafeFetchError(
                        "redirect_no_location", f"{status} without Location"
                    )
                hops += 1
                if hops > max_redirects:
                    raise SafeFetchError(
                        "too_many_redirects", f"exceeded {max_redirects} redirects"
                    )
                _remaining()
                # The new URL re-enters the loop and is fully re-validated
                # (scheme, host allowlist, DNS, IP) before anything connects.
                current = urljoin(current, location)
                continue

            if status != 200:
                raise SafeFetchError("bad_status", f"upstream returned HTTP {status}")

            content_type = (
                (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            )
            if allowed_content_types is not None and not any(
                content_type.startswith(prefix) for prefix in allowed_content_types
            ):
                raise SafeFetchError(
                    "content_type_not_allowed",
                    f"Content-Type {content_type!r} is not allowed here",
                )

            # A Content-Length larger than the cap is refused before a single
            # body byte is read — no reason to spend the bandwidth.
            declared = resp.getheader("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise SafeFetchError(
                    "too_large", f"declared {declared} bytes exceeds {max_bytes} cap"
                )

            chunks: list[bytes] = []
            total = 0
            while True:
                _remaining()
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    # Abort mid-stream: an oversized body is never buffered
                    # whole, whatever Content-Length claimed.
                    raise SafeFetchError(
                        "too_large", f"body exceeds {max_bytes} byte cap"
                    )
                chunks.append(chunk)
            return SafeFetchResult(
                content=b"".join(chunks),
                content_type=content_type,
                final_url=current,
            )
        finally:
            resp.close()


__all__ = [
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TOTAL_DEADLINE",
    "SafeFetchError",
    "SafeFetchResult",
    "fetch_bytes",
    "ip_is_forbidden",
]
