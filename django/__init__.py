"""
Django-specific wrappers for JWT authentication.

This module provides Django middleware, authentication backends, and views
that use the framework-agnostic core logic.

NOTE: To avoid circular imports, import views and auth_views directly:
    from stapel_core.django.views import JWTCookieLoginView
    from stapel_core.django.auth_views import JWTLogoutView, JWTRefreshView, JWTStatusView

For OpenAPI/Swagger utilities:
    from stapel_core.django.openapi import (
        extend_schema, get_error_responses, IronErrorSerializer, ...
    )
"""

from .middleware import JWTAuthMiddleware
from .mcp import (
    build_mcp_schema_view,
    convert_openapi_to_openrpc,
    convert_openapi_to_tools_schema,
)
from .utils import (
    load_user_by_uid,
    setup_centralized_admin_login,
    setup_centralized_admin_logout,
    get_admin_logout_urlpattern,
)
from .health import get_health_urls, register_metrics_exporter
from .fields import CdnImageField, CdnImageListField
from .pagination import (
    AnchorPagination,
    AnchorPaginationSerializer,
    CreatedAtAnchorPagination,
    UpdatedAtAnchorPagination,
    IDAnchorPagination,
)

__all__ = [
    "JWTAuthMiddleware",
    "load_user_by_uid",
    "setup_centralized_admin_login",
    "setup_centralized_admin_logout",
    "get_admin_logout_urlpattern",
    "build_mcp_schema_view",
    "convert_openapi_to_openrpc",
    "convert_openapi_to_tools_schema",
    "get_health_urls",
    "register_metrics_exporter",
    "CdnImageField",
    "CdnImageListField",
    # Pagination
    "AnchorPagination",
    "AnchorPaginationSerializer",
    "CreatedAtAnchorPagination",
    "UpdatedAtAnchorPagination",
    "IDAnchorPagination",
]