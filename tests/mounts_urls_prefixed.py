"""The same service URLconf mounted whole under a path prefix.

This is the monolith-under-prefix deployment shape (the stapel-studio case):
every URL of the project lives below ``myproj/``.
"""
from django.urls import include, path

urlpatterns = [
    path("myproj/", include("tests.mounts_urls")),
]
