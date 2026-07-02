"""HTTP surface for the Function primitive (microservices transport).

The owning service mounts get_function_urls() once; every function it
registered becomes callable at POST {prefix}api/_functions/<name>/ by other
services (service-API-key protected). The registry stays the single source
of truth — the HTTP layer is just a transport adapter around it.
"""
from __future__ import annotations

import logging

from django.urls import path
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..django.api.permissions import IsServiceRequest
from .exceptions import FunctionNotRegistered
from .registry import function_registry

logger = logging.getLogger(__name__)


class FunctionCallView(APIView):
    """Invoke a registered function by name. Service-to-service only."""

    permission_classes = [IsServiceRequest]

    def post(self, request, name: str):
        try:
            handler = function_registry.get(name)
        except FunctionNotRegistered:
            return Response(
                {"error": f"unknown function: {name}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        payload = {}
        if isinstance(request.data, dict):
            payload = request.data.get("payload") or {}

        try:
            function_registry.validate(name, payload)
            result = handler(payload)
        except Exception as exc:
            logger.exception("function %s failed", name)
            return Response(
                {"error": repr(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response({"result": result})


def get_function_urls(url_prefix: str = ""):
    """URL patterns exposing this service's registered functions."""
    return [
        path(
            f"{url_prefix}api/_functions/<str:name>/",
            FunctionCallView.as_view(),
            name="stapel-function-call",
        ),
    ]
