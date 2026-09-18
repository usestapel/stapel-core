"""URLconf fixture for the contract check (``stapel_core.contract.W001``).

One route per shape the check must tell apart. As with the adoption fixture,
the green rows outnumber the red one on purpose: a check that reports a
legitimate plain view under ``api/`` would be silenced wholesale.
"""
from django.http import JsonResponse
from django.urls import include, path
from django.views import View
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView


# --- red: a plain Django View on the API surface --------------------------
class PlainStatusView(View):
    def get(self, request):  # pragma: no cover - never called
        return JsonResponse({})


# --- red: a plain function view on the API surface ------------------------
def plain_function_view(request):  # pragma: no cover - never called
    return JsonResponse({})


# --- red: an exemption that is not one (W002) -----------------------------
class TypoedExemptionView(View):
    stapel_contract_exempt = 1

    def get(self, request):  # pragma: no cover - never called
        return JsonResponse({})


# --- green: a DRF view ----------------------------------------------------
class ProperAPIView(APIView):
    def get(self, request):  # pragma: no cover - never called
        return Response({})


@api_view(["GET"])
def proper_function_view(request):  # pragma: no cover - never called
    return Response({})


# --- green: exempt, with a reason -----------------------------------------
class StreamingExemptView(View):
    stapel_contract_exempt = "server-sent events: DRF renders no stream"

    def get(self, request):  # pragma: no cover - never called
        return JsonResponse({})


# --- green: exempt by the bare marker -------------------------------------
class BareExemptView(View):
    stapel_contract_exempt = True

    def get(self, request):  # pragma: no cover - never called
        return JsonResponse({})


# --- green: a plain view OUTSIDE the API surface --------------------------
class FrontendPageView(View):
    def get(self, request):  # pragma: no cover - never called
        return JsonResponse({})


urlpatterns = [
    path("api/v1/plain/", PlainStatusView.as_view(), name="plain"),
    path("api/v1/plain-fn/", plain_function_view, name="plain-fn"),
    path("api/v1/typoed/", TypoedExemptionView.as_view(), name="typoed"),
    path("api/v1/proper/", ProperAPIView.as_view(), name="proper"),
    path("api/v1/proper-fn/", proper_function_view, name="proper-fn"),
    path("api/v1/stream/", StreamingExemptView.as_view(), name="stream"),
    path("api/v1/bare-exempt/", BareExemptView.as_view(), name="bare-exempt"),
    path("dashboard/", FrontendPageView.as_view(), name="dashboard"),
    # Nested include — the survey recurses, and one view mounted twice is
    # reported once.
    path("nested/", include([
        path("api/v1/plain/", PlainStatusView.as_view(), name="nested-plain"),
    ])),
]
