"""Custom DRF routers for Stapel services."""
from rest_framework.routers import DefaultRouter


class OptionalSlashRouter(DefaultRouter):
    """Router that accepts URLs with or without trailing slash."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.trailing_slash = '/?'
