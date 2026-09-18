"""URLconf fixture: ``JWTStatusView`` mounted where stapel-auth mounts it.

The path shape matters — the contract question is about a route under an API
prefix, and the schema generator is asked about exactly that surface.
"""
from django.urls import include, path

from stapel_core.django.jwt.views import JWTStatusView

urlpatterns = [
    path("api/v1/", include([
        path("jwt/status/", JWTStatusView.as_view(), name="jwt_status"),
    ])),
]
