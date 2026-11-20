"""Profile 2 binds error.404.not_found where DRF's generic lookup can answer it.

``GenericAPIView.get_object`` raises Django's ``Http404`` through
``get_object_or_404``; core's exception handler answers it with
``error.404.not_found``. That path runs inside DRF, which the code walker
does not read, so a retrieve/update/destroy operation used to carry no 404
unless the module declared ``@raises`` by hand.
"""
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.routers import SimpleRouter
from rest_framework.views import APIView

from stapel_core.django.api.errors import ERR_404_NOT_FOUND
from stapel_core.django.openapi.contract import contract_profile
from stapel_core.django.openapi.schemas import PermissionAwareAutoSchema


class UserOut(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = ["id"]


class _Base(viewsets.GenericViewSet):
    schema = PermissionAwareAutoSchema()
    permission_classes = [permissions.AllowAny]
    serializer_class = UserOut

    def get_queryset(self):
        return get_user_model().objects.all()


class ModelViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    mixins.CreateModelMixin,
    _Base,
):
    @action(detail=True, methods=["post"])
    def touch(self, request, pk=None):
        self.get_object()
        return Response({})

    @action(detail=False, methods=["get"])
    def summary(self, request):
        return Response({})


class OverridingViewSet(mixins.RetrieveModelMixin, _Base):
    def get_object(self):
        obj = super().get_object()
        return obj


class ShortcutView(APIView):
    schema = PermissionAwareAutoSchema()
    permission_classes = [permissions.AllowAny]

    @extend_schema(responses={200: UserOut})
    def get(self, request, pk):
        get_object_or_404(get_user_model(), pk=pk)
        return Response({})


class PlainView(APIView):
    schema = PermissionAwareAutoSchema()
    permission_classes = [permissions.AllowAny]

    @extend_schema(responses={200: UserOut})
    def get(self, request):
        return Response({})


def _ops(urlpatterns):
    generator = SchemaGenerator(patterns=urlpatterns)
    with contract_profile(2):
        schema = generator.get_schema(request=None, public=True)
    out = {}
    for path, item in schema["paths"].items():
        for method, op in item.items():
            if isinstance(op, dict):
                codes = op.get("responses", {}).get("404", {}).get("x-stapel-errors", [])
                out[(method, path)] = codes
    return out


def _router(viewset, prefix):
    router = SimpleRouter()
    router.register(prefix, viewset, basename=prefix)
    return router.urls


def test_generic_detail_operations_bind_not_found():
    ops = _ops(_router(ModelViewSet, "things"))
    for key in [
        ("get", "/things/{id}/"),
        ("put", "/things/{id}/"),
        ("patch", "/things/{id}/"),
        ("delete", "/things/{id}/"),
        ("post", "/things/{id}/touch/"),
    ]:
        assert ops[key] == [ERR_404_NOT_FOUND], key


def test_inversion_collection_operations_do_not_bind_not_found():
    ops = _ops(_router(ModelViewSet, "things"))
    for key in [("get", "/things/"), ("post", "/things/"), ("get", "/things/summary/")]:
        assert ops[key] == [], key


def test_an_override_calling_super_get_object_binds_not_found():
    ops = _ops(_router(OverridingViewSet, "others"))
    assert ops[("get", "/others/{id}/")] == [ERR_404_NOT_FOUND]


def test_django_get_object_or_404_binds_not_found():
    from django.urls import path

    ops = _ops([path("short/<int:pk>/", ShortcutView.as_view())])
    assert ops[("get", "/short/{id}/")] == [ERR_404_NOT_FOUND]


def test_inversion_a_view_without_a_lookup_binds_nothing():
    from django.urls import path

    ops = _ops([path("plain/", PlainView.as_view())])
    assert ops[("get", "/plain/")] == []


def test_inversion_profile1_binds_nothing():
    generator = SchemaGenerator(patterns=_router(ModelViewSet, "things"))
    with contract_profile(1):
        schema = generator.get_schema(request=None, public=True)
    op = schema["paths"]["/things/{id}/"]["get"]
    assert "x-stapel-errors" not in op["responses"].get("404", {})
