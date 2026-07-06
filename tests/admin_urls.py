"""Urlconf for the AS-3 admin-visibility tests (direct-URL enforcement)."""
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]
