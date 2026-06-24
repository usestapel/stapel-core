"""
Common OpenAPI/Swagger configuration utilities for Iron services.

This module provides helper functions to configure drf-spectacular with JWT authentication
and custom Swagger UI behavior (logout URL fix, etc.).

IMPORTANT: This module uses lazy imports to avoid triggering DRF/drf-spectacular
imports before Django settings are fully configured. This allows `get_spectacular_settings`
to be safely used in settings.py files.
"""

from django.urls import path, include


def get_spectacular_settings(
    title: str,
    description: str,
    version: str = '1.0.0',
    **extra_settings
) -> dict:
    """
    Get drf-spectacular settings for a service.

    This merges service-specific settings with common defaults.
    Use this in your service's settings.py to configure SPECTACULAR_SETTINGS.

    Args:
        title: API title (e.g., "Iron Authentication API")
        description: API description (markdown supported)
        version: API version string
        **extra_settings: Additional settings to merge

    Returns:
        dict: Complete SPECTACULAR_SETTINGS configuration

    Example:
        # In your service's settings.py:
        from stapel_core.django.swagger import get_spectacular_settings

        SPECTACULAR_SETTINGS = get_spectacular_settings(
            title="Iron Auth API",
            description="Authentication service for Iron platform.",
            version="1.0.0",
        )
    """
    from stapel_core.django.settings import SPECTACULAR_SETTINGS as base_settings

    settings = base_settings.copy()
    settings.update({
        'TITLE': title,
        'DESCRIPTION': description,
        'VERSION': version,
    })
    settings.update(extra_settings)

    return settings


def get_swagger_urls(url_prefix: str = ''):
    """
    Get URL patterns for Swagger UI, ReDoc, and OpenAPI schema.

    This provides the standard URL configuration for drf-spectacular
    with custom Swagger UI that fixes the logout URL.

    Args:
        url_prefix: URL prefix for the endpoints (e.g., 'auth/', 'marketplace/')

    Returns:
        list: URL patterns to include in your urlpatterns

    Example:
        # In your service's urls.py:
        from stapel_core.django.swagger import get_swagger_urls

        urlpatterns = [
            *get_swagger_urls('auth/'),
            # ... other URLs
        ]
    """
    from drf_spectacular.views import (
        SpectacularAPIView,
        SpectacularRedocView,
    )

    # Register JWT authentication extension (safe to call multiple times)
    _register_jwt_auth_extension()

    return [
        # OpenAPI schema endpoint (JSON/YAML)
        path(f'{url_prefix}schema/', SpectacularAPIView.as_view(), name='schema'),

        # Swagger UI with custom JS injection
        path(
            f'{url_prefix}swagger/',
            CustomSpectacularSwaggerView.as_view(url_name='schema'),
            name='swagger-ui'
        ),

        # ReDoc UI
        path(
            f'{url_prefix}redoc/',
            SpectacularRedocView.as_view(url_name='schema'),
            name='redoc'
        ),
    ]


class CustomSpectacularSwaggerView:
    """
    Custom Swagger UI view that injects JavaScript to fix logout URL
    and customize the UI behavior.

    This wraps drf-spectacular's SpectacularSwaggerView and injects
    custom JavaScript after rendering.
    """

    @staticmethod
    def _get_custom_script(current_prefix: str, services: list) -> bytes:
        """Generate custom script with service navigation."""
        # Build services JSON for JS
        import json
        services_json = json.dumps(services)

        return f"""
<style>
.iron-topbar {{
    background: #1f2937;
    padding: 8px 16px;
    display: flex;
    align-items: center;
    gap: 16px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 14px;
    position: sticky;
    top: 0;
    z-index: 1000;
}}
.iron-topbar a {{
    color: #fff;
    text-decoration: none;
}}
.iron-topbar a:hover {{
    text-decoration: underline;
}}
.iron-admin-btn {{
    background: #417690;
    color: #fff !important;
    padding: 6px 12px;
    border-radius: 4px;
    font-weight: 500;
}}
.iron-admin-btn:hover {{
    background: #205067;
    text-decoration: none !important;
}}
.iron-svc-dropdown {{
    position: relative;
}}
.iron-svc-btn {{
    background: rgba(255,255,255,0.1);
    border: 1px solid rgba(255,255,255,0.2);
    color: #fff;
    padding: 6px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 14px;
}}
.iron-svc-btn:hover {{
    background: rgba(255,255,255,0.2);
}}
.iron-svc-menu {{
    display: none;
    position: absolute;
    top: calc(100% - 4px);
    left: 0;
    background: #fff;
    min-width: 260px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    border-radius: 6px;
    padding-top: 8px;
    z-index: 1001;
}}
.iron-svc-dropdown:hover .iron-svc-menu,
.iron-svc-menu:hover {{
    display: block;
}}
.iron-svc-menu-title {{
    padding: 8px 16px 4px;
    font-size: 11px;
    font-weight: 600;
    color: #666;
    text-transform: uppercase;
}}
.iron-svc-item {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 16px;
    color: #333;
}}
.iron-svc-item:hover {{
    background: #f5f5f5;
}}
.iron-svc-item.active {{
    background: #e8f4f8;
    font-weight: 600;
}}
.iron-svc-item .links {{
    display: flex;
    gap: 6px;
}}
.iron-svc-item .links a {{
    color: #417690;
    padding: 2px 8px;
    border-radius: 3px;
    background: #f0f0f0;
    font-size: 12px;
}}
.iron-svc-item .links a:hover {{
    background: #417690;
    color: #fff;
    text-decoration: none;
}}
</style>
<script>
(function() {{
    const currentPrefix = '{current_prefix}';
    const services = {services_json};

    function createNavbar() {{
        if (document.getElementById('iron-topbar')) return;

        const topbar = document.createElement('div');
        topbar.id = 'iron-topbar';
        topbar.className = 'iron-topbar';

        const adminUrl = currentPrefix ? '/' + currentPrefix + '/admin/' : '/admin/';

        let servicesHtml = '';
        services.forEach(svc => {{
            const isActive = svc.prefix === currentPrefix ? ' active' : '';
            servicesHtml += '<div class="iron-svc-item' + isActive + '">' +
                '<span>' + svc.name + '</span>' +
                '<span class="links">' +
                    '<a href="' + svc.admin_url + '">Admin</a>' +
                    '<a href="' + svc.swagger_url + '">API</a>' +
                '</span>' +
            '</div>';
        }});

        topbar.innerHTML = '<a href="' + adminUrl + '" class="iron-admin-btn">Admin</a>' +
            '<div class="iron-svc-dropdown">' +
                '<button class="iron-svc-btn">Services ▾</button>' +
                '<div class="iron-svc-menu">' +
                    '<div class="iron-svc-menu-title">All Services</div>' +
                    servicesHtml +
                    '<div class="iron-svc-menu-title" style="margin-top: 8px; border-top: 1px solid #eee; padding-top: 12px;">Tools</div>' +
                    '<div class="iron-svc-item">' +
                        '<span>Translator Dashboard</span>' +
                        '<span class="links"><a href="/translate/dashboard/">Open</a></span>' +
                    '</div>' +
                    '<div class="iron-svc-menu-title" style="margin-top: 8px; border-top: 1px solid #eee; padding-top: 12px;">Monitoring</div>' +
                    '<div class="iron-svc-item">' +
                        '<span>Grafana</span>' +
                        '<span class="links"><a href="/monitoring/grafana/d/iron-home/iron-system-overview" target="_blank">Open</a></span>' +
                    '</div>' +
                    '<div class="iron-svc-item">' +
                        '<span>Prometheus</span>' +
                        '<span class="links"><a href="/monitoring/prometheus/" target="_blank">Open</a></span>' +
                    '</div>' +
                '</div>' +
            '</div>';

        document.body.insertBefore(topbar, document.body.firstChild);
    }}

    function fixLogoutUrl() {{
        const logoutLink = document.querySelector('a[href*="/accounts/logout"]');
        if (logoutLink) {{
            logoutLink.href = '/auth/admin/logout/';
        }}
    }}

    if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', function() {{
            createNavbar();
            fixLogoutUrl();
        }});
    }} else {{
        createNavbar();
        fixLogoutUrl();
    }}

    const observer = new MutationObserver(fixLogoutUrl);
    observer.observe(document.body, {{ childList: true, subtree: true }});
}})();
</script>
""".encode('utf-8')

    @classmethod
    def as_view(cls, **initkwargs):
        """Create a view that wraps SpectacularSwaggerView with custom JS injection."""
        from drf_spectacular.views import SpectacularSwaggerView
        from django.views.decorators.cache import never_cache

        get_custom_script = cls._get_custom_script

        class WrappedSwaggerView(SpectacularSwaggerView):
            """Swagger view with custom JavaScript injection."""

            def dispatch(self, request, *args, **kwargs):
                response = super().dispatch(request, *args, **kwargs)

                # Render the response if it's a TemplateResponse
                if hasattr(response, 'render'):
                    response.render()

                # Inject custom JavaScript with service navigation
                if hasattr(response, 'content') and b'swagger-ui' in response.content:
                    from django.conf import settings
                    from stapel_core.core.config import IRON_SERVICES

                    current_prefix = getattr(settings, 'URL_PREFIX', '').rstrip('/')
                    services = []
                    for svc in IRON_SERVICES:
                        prefix = svc['prefix']
                        services.append({
                            'name': svc['name'],
                            'prefix': prefix,
                            'admin_url': f"/{prefix}/admin/" if prefix else "/admin/",
                            'swagger_url': f"/{prefix}/swagger/" if prefix else "/swagger/",
                        })

                    custom_script = get_custom_script(current_prefix, services)
                    response.content = response.content.replace(
                        b'</body>',
                        custom_script + b'</body>'
                    )

                return response

        # Apply never_cache decorator to the view function
        view = WrappedSwaggerView.as_view(**initkwargs)
        return never_cache(view)


class IsStaffUserForSwagger:
    """
    Permission class for Swagger/OpenAPI documentation access.

    Only allows authenticated staff users to access Swagger UI and ReDoc.
    This ensures API documentation is only visible to internal staff members.

    Note: This class inherits from BasePermission lazily to avoid importing
    rest_framework at module load time.
    """

    def __new__(cls):
        """Create permission class that inherits from BasePermission."""
        from rest_framework import permissions

        class _IsStaffUserForSwagger(permissions.BasePermission):
            """Permission class for Swagger access."""

            def has_permission(self, request, _view):
                """Check if user is authenticated and is staff."""
                return bool(
                    request.user and
                    request.user.is_authenticated and
                    request.user.is_staff
                )

        return _IsStaffUserForSwagger()


def _register_jwt_auth_extension():
    """
    Register the JWT cookie authentication extension with drf-spectacular.

    This function should be called after Django settings are configured,
    typically in a urls.py or apps.py ready() method.
    """
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class JWTCookieAuthenticationExtension(OpenApiAuthenticationExtension):
        """
        OpenAPI extension for JWTCookieAuthentication.

        This tells drf-spectacular how to document the JWT cookie authentication
        in the generated OpenAPI schema.
        """
        target_class = 'stapel_core.django.authentication.JWTCookieAuthentication'
        name = 'JWTCookieAuth'

        def get_security_definition(self, _auto_schema):
            return {
                'type': 'apiKey',
                'in': 'cookie',
                'name': 'iron_jwt',
                'description': 'JWT token stored in cookie. Login via /auth/admin/ to get the cookie.',
            }

    # The extension is auto-registered when the class is defined
    return JWTCookieAuthenticationExtension


def get_dev_urls(url_prefix: str = '', mcp_schema_view=None):
    """
    Get development-only URL patterns (Swagger, MCP, Debug Toolbar).

    Returns empty list in production. Use this for all dev tools.

    Args:
        url_prefix: URL prefix for the endpoints (e.g., 'auth/')
        mcp_schema_view: Optional MCP schema view to include

    Returns:
        list: URL patterns (empty in production)

    Example:
        from stapel_core.django.swagger import get_dev_urls
        from stapel_core.django.mcp import build_mcp_schema_view

        mcp_schema_view = build_mcp_schema_view(...)

        urlpatterns = [
            *get_dev_urls(url_prefix, mcp_schema_view),
            # ... production URLs
        ]
    """
    import os
    env = os.environ.get('DJANGO_ENV', '')

    if env not in ('local', 'dev'):
        return []

    urls = [
        # Swagger/OpenAPI
        *get_swagger_urls(url_prefix),
    ]

    # MCP Schema endpoints
    if mcp_schema_view:
        urls.extend([
            path(f'{url_prefix}mcp-schema.json', mcp_schema_view, name='mcp-schema'),
            path(f'{url_prefix}.well-known/mcp.json', mcp_schema_view, name='mcp-wellknown'),
        ])

    # Debug Toolbar (only if in INSTALLED_APPS)
    from django.conf import settings
    if 'debug_toolbar' in settings.INSTALLED_APPS:
        import debug_toolbar
        urls.append(path(f'{url_prefix}__debug__/', include(debug_toolbar.urls)))

    return urls
