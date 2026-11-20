"""Verified claims of the access token a request is authenticated with.

Any brick can ask which session a request belongs to without importing the
session authority (stapel-auth): the issuer signs the session id into the
token (:data:`SESSION_CLAIM`), and every service holding the verification key
reads it here.

The token is verified again — signature, expiry, revocation, user ban —
through :meth:`jwt_provider.validate_token`, the same gate the authentication
classes use, and its ``user_id`` must name ``request.user``. A request
authenticated some other way (Django session, service key) has no token
claims, whatever headers it also carries.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from stapel_core.core.jwt_handler import SESSION_CLAIM

__all__ = ["SESSION_CLAIM", "request_token_claims", "request_session_id"]

#: Session ids are UUID strings at the issuer; anything longer is not one.
SESSION_ID_MAX = 64

_CACHE_ATTR = "_stapel_token_claims"
_MISSING = object()


def _http_request(request):
    """The Django ``HttpRequest`` behind a DRF ``Request`` (or itself)."""
    return getattr(request, "_request", request)


def _verified_claims(raw, user) -> Optional[Dict[str, Any]]:
    from .provider import jwt_provider
    from .utils import extract_jwt_from_request

    presented, _ = extract_jwt_from_request(raw)
    # The middleware re-minted an expired access token from the refresh
    # token on this very request; that token is the one it authenticated.
    reminted = getattr(raw, "_new_access_token", None)
    for token in (presented, reminted):
        if not token:
            continue
        claims = jwt_provider.validate_token(token)
        if claims and str(claims.get("user_id") or "") == str(user.pk):
            return dict(claims)
    return None


def request_token_claims(request) -> Optional[Dict[str, Any]]:
    """The verified claims of the access token ``request.user`` came from.

    ``None`` when the request is not authenticated, carries no valid access
    token, or its token speaks for someone other than ``request.user``.
    Registered claims (``exp``, ``iat``, ``jti``, ``token_type``) are not
    included. Computed once per request.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    raw = _http_request(request)
    key = str(user.pk)
    cached = getattr(raw, _CACHE_ATTR, _MISSING)
    if cached is not _MISSING and cached[0] == key:
        claims = cached[1]
    else:
        claims = _verified_claims(raw, user)
        try:
            setattr(raw, _CACHE_ATTR, (key, claims))
        except AttributeError:  # a request object that refuses attributes
            pass
    return dict(claims) if claims is not None else None


def request_session_id(request) -> Optional[str]:
    """The issuer's session id signed into the request's access token.

    ``None`` when there is no verified token, or the token predates the claim
    (or was minted outside a session) — callers fall back to whatever they did
    before the claim existed, and must not read ``None`` as "some session".
    """
    claims = request_token_claims(request)
    if not claims:
        return None
    sid = claims.get(SESSION_CLAIM)
    if not isinstance(sid, str):
        return None
    sid = sid.strip()
    if not sid or len(sid) > SESSION_ID_MAX:
        return None
    return sid
