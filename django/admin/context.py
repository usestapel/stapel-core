"""
Context processor for Django admin to add service navigation.
"""

from django.conf import settings


def stapel_services(_request):
    """
    Add Stapel services navigation to admin context.

    This context processor adds a list of available services with their URLs
    to the admin template context, enabling cross-service navigation.

    Services are defined in stapel_core.core.config.STAPEL_SERVICES.
    """
    from stapel_core.core.config import STAPEL_SERVICES

    # Get current service prefix
    current_prefix = getattr(settings, 'URL_PREFIX', '').rstrip('/')

    # Build services list with URLs and active status
    services = []
    for service in STAPEL_SERVICES:
        prefix = service['prefix']
        admin_url = f"/{prefix}/admin/" if prefix else "/admin/"
        swagger_url = f"/{prefix}/swagger/" if prefix else "/swagger/"

        services.append({
            'name': service['name'],
            'admin_url': admin_url,
            'swagger_url': swagger_url,
            'prefix': prefix,
            'is_active': current_prefix == prefix or (not current_prefix and not prefix),
        })

    # Current service swagger URL
    current_swagger_url = f"/{current_prefix}/swagger/" if current_prefix else "/swagger/"

    # Dashboard URL (only for services that have dashboards)
    # Currently only translate service has a dashboard
    dashboard_urls = {
        'translate': '/translate/dashboard/',
    }
    current_dashboard_url = dashboard_urls.get(current_prefix)

    return {
        'stapel_services': services,
        'current_swagger_url': current_swagger_url,
        'current_service_prefix': current_prefix,
        'current_dashboard_url': current_dashboard_url,
    }

