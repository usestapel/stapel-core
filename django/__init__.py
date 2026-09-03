"""
Django-specific wrappers for JWT authentication.

This module provides Django middleware, authentication backends, and views
that use the framework-agnostic core logic.

NOTE: To avoid circular imports, import views and auth_views directly:
    from stapel_core.django.jwt.login_views import JWTCookieLoginView
    from stapel_core.django.jwt.views import JWTLogoutView, JWTRefreshView, JWTStatusView

For OpenAPI/Swagger utilities:
    from stapel_core.django.openapi.schemas import (
        extend_schema, get_error_responses, StapelErrorSerializer, ...
    )
"""

from .api.pagination import (
    AnchorPagination,
    AnchorPaginationSerializer,
    CreatedAtAnchorPagination,
    IDAnchorPagination,
    UpdatedAtAnchorPagination,
)
from .cdn.fields import CdnImageField, CdnImageListField
from .fieldspec import FieldSpec, FieldSpecError
from .jwt.middleware import JWTAuthMiddleware
from .jwt.utils import (
    get_admin_logout_urlpattern,
    load_user_by_uid,
    setup_centralized_admin_login,
)
from .monitoring.health import get_health_urls, register_metrics_exporter
from .monitoring.version import (
    build_info,
    get_version_urls,
    installed_libraries,
)
from .openapi.mcp import (
    build_mcp_schema_view,
    convert_openapi_to_openrpc,
    convert_openapi_to_tools_schema,
)

__all__ = [
    "JWTAuthMiddleware",
    "load_user_by_uid",
    "setup_centralized_admin_login",
    "get_admin_logout_urlpattern",
    "build_mcp_schema_view",
    "convert_openapi_to_openrpc",
    "convert_openapi_to_tools_schema",
    "get_health_urls",
    "register_metrics_exporter",
    # "which build is this?" — monitoring/version.py
    "get_version_urls",
    "installed_libraries",
    "build_info",
    "CdnImageField",
    "CdnImageListField",
    # Copy-seam field partition
    "FieldSpec",
    "FieldSpecError",
    # Pagination
    "AnchorPagination",
    "AnchorPaginationSerializer",
    "CreatedAtAnchorPagination",
    "UpdatedAtAnchorPagination",
    "IDAnchorPagination",
]
