"""Project-scoped short-lived scope tokens.

Contract decision — **opaque, stored hashed** (not signed/JWT): the same
trade-off the flow-mcp design settled on — tokens are few (one per
container per start), verification is a single indexed lookup in the
control plane's own DB, and *instant revocation* (rotation, container
stop, incident) matters more than saving that lookup. A signed token
would need a revocation list anyway, which is this table.

Properties:

- 256-bit random secret, ``sgw_`` prefix (secret-scanner friendly);
- only the **sha256 hex** is stored — a DB leak does not leak live tokens;
- bound to a ``project`` (mandatory), optionally to a ``container`` and a
  ``network`` (exact IP or CIDR) for the network-identity check;
- short-lived (``TOKEN_TTL``, default 1h); expiry is checked on every use;
- rotation issues a fresh secret with the same bindings and kills the old
  one (optionally after a small grace window for in-flight requests).
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from .conf import gateway_settings
from .exceptions import TokenInvalid

TOKEN_PREFIX = "sgw_"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """The plaintext secret leaves the gateway exactly once — here."""

    token: str
    token_id: int
    project: str
    container: str | None
    network: str | None
    expires_at: datetime


def issue_token(
    project: str,
    *,
    container: str | None = None,
    network: str | None = None,
    ttl: int | None = None,
) -> IssuedToken:
    """Mint a scope token for *project*.

    ``network`` pins the token to the caller's network identity (exact IP
    or CIDR) — the container-manager passes the container's address here
    so the default verifier can enforce "a request about project X comes
    from container X".
    """
    from stapel_core.django.gateway.models import ScopeToken

    if not project:
        raise ValueError("a scope token is project-scoped: project is required")
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires_at = timezone.now() + timedelta(seconds=int(ttl or gateway_settings.TOKEN_TTL))
    row = ScopeToken.objects.create(
        token_hash=_hash(raw),
        project=project,
        container=container,
        network=network,
        expires_at=expires_at,
    )
    return IssuedToken(
        token=raw,
        token_id=row.id,
        project=project,
        container=container,
        network=network,
        expires_at=expires_at,
    )


def verify_token(raw: str | None, *, project: str | None = None):
    """Resolve a presented token to its live :class:`ScopeToken` row.

    Raises :class:`TokenInvalid` with a specific ``reason`` (``missing`` /
    ``unknown`` / ``revoked`` / ``expired`` / ``project_mismatch``) — the
    audit line carries it; HTTP callers get a uniform 401 either way.
    """
    from stapel_core.django.gateway.models import ScopeToken

    if not raw:
        raise TokenInvalid("no scope token presented", reason="token_missing")
    row = ScopeToken.objects.filter(token_hash=_hash(raw)).first()
    if row is None:
        raise TokenInvalid("unknown scope token", reason="token_unknown")
    if row.revoked_at is not None:
        raise TokenInvalid("scope token is revoked", reason="token_revoked")
    if row.expires_at <= timezone.now():
        raise TokenInvalid("scope token is expired", reason="token_expired")
    if project is not None and row.project != project:
        # A valid token for project A does not authorize talk about B.
        raise TokenInvalid(
            "scope token is issued for a different project",
            reason="token_project_mismatch",
        )
    return row


def rotate_token(raw: str, *, ttl: int | None = None, grace: int = 0) -> IssuedToken:
    """Issue a fresh token with the old one's bindings; retire the old.

    ``grace`` (seconds) keeps the old token alive briefly so in-flight
    requests do not fail mid-rotation; ``0`` revokes immediately.
    """
    old = verify_token(raw)
    fresh = issue_token(
        old.project,
        container=old.container,
        network=old.network,
        ttl=ttl,
    )
    now = timezone.now()
    if grace > 0:
        old.expires_at = min(old.expires_at, now + timedelta(seconds=grace))
        old.save(update_fields=["expires_at"])
    else:
        old.revoked_at = now
        old.save(update_fields=["revoked_at"])
    return fresh


def revoke_token(raw_or_id: str | int) -> bool:
    """Revoke by plaintext token or by row id. True if a live row changed."""
    from stapel_core.django.gateway.models import ScopeToken

    if isinstance(raw_or_id, int):
        qs = ScopeToken.objects.filter(id=raw_or_id)
    else:
        qs = ScopeToken.objects.filter(token_hash=_hash(raw_or_id))
    return bool(qs.filter(revoked_at__isnull=True).update(revoked_at=timezone.now()))


def purge_expired_tokens(*, older_than: datetime | None = None) -> int:
    """Delete rows expired/revoked before *older_than* (default: now)."""
    from django.db.models import Q

    from stapel_core.django.gateway.models import ScopeToken

    cutoff = older_than or timezone.now()
    deleted, _ = ScopeToken.objects.filter(
        Q(expires_at__lt=cutoff) | Q(revoked_at__lt=cutoff)
    ).delete()
    return deleted


__all__ = [
    "TOKEN_PREFIX",
    "IssuedToken",
    "issue_token",
    "purge_expired_tokens",
    "revoke_token",
    "rotate_token",
    "verify_token",
]
