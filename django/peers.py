"""HTTP clients for sibling Stapel services — routing 404s are never verdicts.

Two bugs keep recurring whenever one service calls another over HTTP, and
they compose into a silent outage:

1. the caller **hardcodes the peer's path**, so the day the peer moves its
   API (the §60 ``v1/`` canon sweep moved every module's surface under
   ``api/v1/``) the caller keeps asking for a path that no longer exists;
2. the caller **reads the resulting 404 as an answer** — "not a member",
   "no keys", "nothing there" — because a routing 404 and a view's own 404
   are indistinguishable by status code.

Together they turn a deploy-time skew into a wrong business verdict that
nobody sees in the logs. This module is the antidote, extracted from
``stapel_core.django.workspaces`` (owner-reported live incident, 2026-07-26,
where the workspaces membership client told a workspace's own OWNER it was
not a member for weeks):

- :func:`service_answered` tells a view's answer from the URL resolver's, by
  Content-Type — DRF renders ``application/json``, Django's resolver (and
  any proxy in front of it) renders an HTML error page;
- :func:`get_with_path_discovery` tries a list of candidate mount points
  newest-first, remembers the one that answered, and raises
  :class:`PeerRouteUnavailable` when *none* of them reached a view — an
  explicit "this service and its peer disagree about the path", never a
  silent empty result.

A caller that catches nothing still gets a loud failure; a caller that wants
to degrade catches :class:`PeerRouteUnavailable` specifically and can say so.
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

import requests

logger = logging.getLogger(__name__)

__all__ = [
    "PeerRouteUnavailable",
    "PathResolver",
    "service_answered",
    "get_with_path_discovery",
]


class PeerRouteUnavailable(Exception):
    """No candidate path reached the peer's *view*.

    Distinct from any answer the peer gave. Raised when every candidate mount
    point produced a 404 from the URL resolver — i.e. this client and the
    peer service disagree about where the endpoint lives. Callers must not
    translate this into a business verdict ("no keys", "not a member"): it is
    the HTTP equivalent of a compile error between two deployed versions.
    """


def service_answered(resp) -> bool:
    """Did the VIEW answer, or did the URL resolver?

    A 404 rendered by DRF is a real answer from the endpoint ("that object
    does not exist") and carries a JSON body. A 404 from Django's URL
    resolver — or from a proxy in front of it — carries an HTML debug/error
    page. They are indistinguishable by status code alone, which is exactly
    how a client/server path skew becomes a wrong verdict. The content type
    tells them apart.
    """
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    return content_type == "application/json"


class PathResolver:
    """Remembers which candidate path last answered, per client instance.

    A module-level resolver makes the discovery probe cost a single extra
    request per process rather than per call. Callers that want no memory
    (tests, one-shot scripts) simply pass ``resolver=None``.
    """

    def __init__(self, candidates: Sequence[str]):
        if not candidates:
            raise ValueError("PathResolver needs at least one candidate path")
        self.candidates: Tuple[str, ...] = tuple(candidates)
        self.resolved: Optional[str] = None

    def ordered(self) -> Tuple[str, ...]:
        if not self.resolved:
            return self.candidates
        return (self.resolved,) + tuple(
            c for c in self.candidates if c != self.resolved
        )

    def remember(self, path: str) -> None:
        if path != self.resolved:
            self.resolved = path
            logger.info("peer route resolved at %s", path)


def get_with_path_discovery(
    base_url: str,
    candidates: Iterable[str],
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 15.0,
    resolver: Optional[PathResolver] = None,
) -> Tuple[object, str]:
    """GET ``base_url + path`` for the first candidate path that reaches a view.

    ``candidates`` are absolute paths on the peer, newest mount point first
    (``("/x/api/v1/keys/", "/x/api/keys/")``). Returns ``(response, url)``.

    Raises :class:`PeerRouteUnavailable` when every candidate 404s from the
    URL resolver — the caller is asking for a path this peer does not serve.
    Transport errors (``requests.RequestException``) propagate unchanged: a
    connection refused is not a routing problem, and trying another path
    cannot help.
    """
    paths: Tuple[str, ...]
    if resolver is not None:
        paths = resolver.ordered()
    else:
        paths = tuple(candidates)
    if not paths:
        raise ValueError("get_with_path_discovery needs at least one candidate path")

    base = str(base_url).rstrip("/")
    request_headers: Dict[str, str] = {"Accept": "application/json"}
    request_headers.update(dict(headers or {}))

    tried = []
    for path in paths:
        url = f"{base}/{path.lstrip('/')}"
        resp = requests.get(url, headers=request_headers, timeout=timeout)
        if resp.status_code == 404 and not service_answered(resp):
            tried.append(url)
            continue  # wrong mount point — try the next candidate
        if resolver is not None:
            resolver.remember(path)
        return resp, url

    raise PeerRouteUnavailable(
        f"no known mount point answered at {base} — this client and the peer "
        f"service disagree about the path. Tried: {', '.join(tried)}"
    )
