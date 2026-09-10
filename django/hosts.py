"""Is a host name somebody's laptop, or something the world can reach.

One question, asked by every check that has to decide whether a relaxation is
still local. ``ALLOWED_HOSTS`` is the only statement a Django process makes
about where it answers, and a check that reads it needs a shared answer to
"does this entry look public?" rather than one classifier per library.

Coarse on purpose, and coarse in a known direction: the private-range prefixes
are matched as text, so a name is called local whenever it plausibly is. The
callers are checks whose finding is a refusal, and a classifier that guessed
"public" on a developer's machine would make those checks noise.
"""
from __future__ import annotations

__all__ = ["LOCAL_HOSTS", "looks_public"]

#: Host values that are never reachable from anywhere else. ``""`` is in the
#: set because an empty ``ALLOWED_HOSTS`` entry is not a host.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "testserver", ""})


def looks_public(host: str) -> bool:
    """Does *host* name something reachable beyond a developer machine?

    ``"*"`` is NOT special-cased here — it is not a host name. A caller
    reading ``ALLOWED_HOSTS`` decides what a wildcard means for its own
    finding (for mock credentials it counts as public: a deployment that
    answers on any Host header is not somebody's laptop).
    """
    host = (host or "").strip().lower().rstrip(".")
    if host in LOCAL_HOSTS:
        return False
    if host.endswith(".local") or host.endswith(".localhost"):
        return False
    if host.startswith("192.168.") or host.startswith("10.") or host.startswith("172."):
        return False
    return True
