"""HTTP surface for project containers.

The one door an untrusted container may knock on::

    POST {prefix}api/_gateway/<verb>/
    Authorization: Bearer sgw_...        (or X-Gateway-Token: sgw_...)
    {"args": {...}, "project": "optional cross-check"}

Authorization is three-factor (system-design §5.9): the project id is
addressing, the scope token is the right to speak, and the network
identity ties the request to the project's container. Token and network
failures are audited like every other refusal (S6) — with whatever
identity is known at that point.

Status mapping: 200 result · 202 parked pending confirmation · 400 args
violate schema · 401 token missing/invalid · 403 network or policy denial
· 404 verb not declared (deny-by-default — no capability enumeration) ·
429 rate limited · 502 handler failed · 500 gateway misconfigured/audit
failure.

Confirmation is deliberately absent from this surface: a container (and
any hijacked agent inside it) must not confirm its own privileged action.
"""
from __future__ import annotations

import logging

from django.urls import path
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import service
from .base import CallerContext, PendingConfirmation
from .exceptions import (
    ArgsInvalid,
    AuditFailure,
    GatewayConfigError,
    HandlerError,
    NetworkMismatch,
    PolicyDenied,
    RateLimited,
    TokenInvalid,
    VerbNotDeclared,
)
from .network import verify_network
from .tokens import verify_token

logger = logging.getLogger(__name__)

_STATUS_BY_ERROR = (
    (RateLimited, status.HTTP_429_TOO_MANY_REQUESTS),
    (PolicyDenied, status.HTTP_403_FORBIDDEN),
    (VerbNotDeclared, status.HTTP_404_NOT_FOUND),
    (ArgsInvalid, status.HTTP_400_BAD_REQUEST),
    (TokenInvalid, status.HTTP_401_UNAUTHORIZED),
    (NetworkMismatch, status.HTTP_403_FORBIDDEN),
    (HandlerError, status.HTTP_502_BAD_GATEWAY),
    (AuditFailure, status.HTTP_500_INTERNAL_SERVER_ERROR),
    (GatewayConfigError, status.HTTP_500_INTERNAL_SERVER_ERROR),
)


def _extract_token(request) -> str | None:
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip() or None
    return request.META.get("HTTP_X_GATEWAY_TOKEN") or None


class GatewayInvokeView(APIView):
    """Invoke a verb with a scope token. The container-facing door."""

    # The scope token *is* the authentication; no session/JWT applies here.
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request, name: str):
        from . import audit

        ip = request.META.get("REMOTE_ADDR")
        body = request.data if isinstance(request.data, dict) else {}
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        caller = CallerContext(channel="http", ip=ip)

        # Factor 2: the scope token.
        try:
            token = verify_token(_extract_token(request), project=body.get("project"))
        except TokenInvalid as exc:
            audit.record(verb=name, decision="denied", caller=caller,
                         args=args, reason=exc.reason)
            return Response({"error": "invalid or missing scope token"},
                            status=status.HTTP_401_UNAUTHORIZED)

        caller = CallerContext(
            channel="http",
            project=token.project,
            container=token.container,
            ip=ip,
            token_id=token.id,
        )

        # Factor 3: network identity — the request about project X must
        # come from the network bound to project X's token.
        if not verify_network(ip, token):
            audit.record(verb=name, decision="denied", caller=caller,
                         args=args, reason=NetworkMismatch.reason)
            return Response({"error": "network identity check failed"},
                            status=status.HTTP_403_FORBIDDEN)

        try:
            result = service.invoke(name, args, caller=caller)
        except Exception as exc:
            return self._error_response(name, exc)

        if isinstance(result, PendingConfirmation):
            return Response(
                {
                    "status": "pending",
                    "confirmation_id": result.confirmation_id,
                    "expires_at": result.expires_at.isoformat(),
                },
                status=status.HTTP_202_ACCEPTED,
            )
        return Response({"result": result})

    def _error_response(self, name: str, exc: Exception) -> Response:
        for err_type, code in _STATUS_BY_ERROR:
            if isinstance(exc, err_type):
                if code >= 500:
                    logger.exception("gateway verb %s failed", name)
                # 404 for undeclared verbs carries no hint of what exists.
                detail = ("unknown verb" if isinstance(exc, VerbNotDeclared)
                          else str(exc))
                return Response({"error": detail}, status=code)
        logger.exception("gateway verb %s failed unexpectedly", name)
        return Response({"error": "gateway failure"},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def get_gateway_urls(url_prefix: str = ""):
    """URL patterns exposing the container-facing gateway endpoint."""
    return [
        path(
            f"{url_prefix}api/_gateway/<str:name>/",
            GatewayInvokeView.as_view(),
            name="stapel-gateway-invoke",
        ),
    ]


__all__ = ["GatewayInvokeView", "get_gateway_urls"]
