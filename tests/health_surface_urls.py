"""URLconf fixture: the probe surface every service mounts."""
from stapel_core.django.monitoring.health import get_health_urls

urlpatterns = get_health_urls()
