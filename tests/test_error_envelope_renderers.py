"""Every error envelope renders under every renderer a view may negotiate.

An anonymous request for the staff-only schema answered 500 instead of 401:
the 401 envelope carried DRF's ``ErrorDetail`` in ``params.detail`` and the
schema's YAML renderer (safe_dump) cannot represent it.
"""
import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy
from drf_spectacular.renderers import OpenApiJsonRenderer, OpenApiYamlRenderer
from drf_spectacular.views import SpectacularAPIView
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    Throttled,
)
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIRequestFactory
from stapel_core.django.api.errors import (
    StapelErrorResponse,
    StapelServiceError,
    StapelValidationError,
    stapel_exception_handler,
)

_factory = APIRequestFactory()

RENDERERS = [JSONRenderer, OpenApiJsonRenderer, OpenApiYamlRenderer]

EXCEPTIONS = [
    NotAuthenticated(),
    AuthenticationFailed(),
    PermissionDenied(),
    NotFound(),
    Throttled(wait=12),
    DRFValidationError({"name": ["This field is required."], "items": [{"qty": ["bad"]}]}),
    DRFValidationError(["flat message"]),
    DjangoValidationError({"name": [gettext_lazy("This field is required.")]}),
    StapelValidationError("error.400.bad_request"),
    StapelServiceError(409, "error.409.conflict", {"detail": gettext_lazy("busy")}),
]


def _ctx():
    return {"request": _factory.get("/"), "view": None}


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda r: r.__name__)
@pytest.mark.parametrize("exc", EXCEPTIONS, ids=lambda e: type(e).__name__)
def test_every_tier_renders(renderer, exc):
    response = stapel_exception_handler(exc, _ctx())
    body = renderer().render(response.data, renderer.media_type)
    assert b"localizable_error" in body


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda r: r.__name__)
def test_error_response_params_render(renderer):
    response = StapelErrorResponse(400, "error.400.bad_request",
                                   {"detail": gettext_lazy("lazy"), "n": (1, 2)})
    body = renderer().render(response.data, renderer.media_type)
    assert b"lazy" in body


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_anonymous_schema_request_is_401_not_500(fmt):
    """The real path: core's staff-only SERVE_PERMISSIONS and JWT cookie
    authentication, no credentials."""
    from stapel_core.django.jwt.authentication import JWTCookieAuthentication
    from stapel_core.django.openapi.swagger import IsStaffUserForSwagger

    view = SpectacularAPIView.as_view(
        permission_classes=[IsStaffUserForSwagger],
        authentication_classes=[JWTCookieAuthentication],
    )
    response = view(_factory.get(f"/schema/?format={fmt}"))
    response.render()
    assert response.status_code == 401, response.status_code
    assert b"error.40" in response.content
