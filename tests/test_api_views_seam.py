"""The serializer seam and the thin-view base — the semantics consumers rely on.

Nineteen stapel modules hand-wrote this seam before the core shipped it
(``docs/reference/module-extension-gaps.md``). These tests pin the contract
those copies established, so a module deleting its local copy inherits the
same behaviour and not merely the same method names:

* attributes default to ``None`` and mean "this direction carries no
  serializer" — never an error;
* the getters, not the attributes, are the read path, so a subclass may swap a
  class attribute OR override a getter for a per-request decision;
* override resolution follows normal MRO — the seam adds no registry, no
  metaclass and no ordering surprise;
* the mixin does not define (and therefore cannot shadow) DRF's own
  ``get_serializer_class``, which is what keeps ViewSet-based modules safe.
"""
import pytest
from django.core.exceptions import ImproperlyConfigured
from rest_framework import serializers as drf_serializers
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from stapel_core.django.api.errors import StapelResponse
from stapel_core.django.api.views import SerializerSeamMixin, StapelAPIView


class EchoRequestSerializer(drf_serializers.Serializer):
    name = drf_serializers.CharField()
    count = drf_serializers.IntegerField(required=False, default=1)


class EchoResponseSerializer(drf_serializers.Serializer):
    name = drf_serializers.CharField()


class LoudResponseSerializer(drf_serializers.Serializer):
    name = drf_serializers.SerializerMethodField()

    def get_name(self, obj):
        return str(obj["name"]).upper()


factory = APIRequestFactory()


def drf_post(payload):
    """A DRF ``Request`` (``.data``), the shape a view method actually gets."""
    return Request(factory.post("/", payload, format="json"), parsers=[JSONParser()])


# ---------------------------------------------------------------------------
# SerializerSeamMixin — attributes, getters, override resolution
# ---------------------------------------------------------------------------


class TestSeamDefaults:
    def test_both_directions_default_to_none(self):
        class V(SerializerSeamMixin):
            pass

        view = V()
        assert view.request_serializer_class is None
        assert view.response_serializer_class is None
        assert view.get_request_serializer_class() is None
        assert view.get_response_serializer_class() is None

    def test_none_is_a_value_not_an_error(self):
        """A view that serializes only one direction says nothing about the other."""

        class V(SerializerSeamMixin):
            response_serializer_class = EchoResponseSerializer

        view = V()
        assert view.get_response_serializer_class() is EchoResponseSerializer
        assert view.get_request_serializer_class() is None

    def test_mixin_carries_no_state(self):
        """Two instances never see each other's classes (no shared registry)."""

        class V(SerializerSeamMixin):
            response_serializer_class = EchoResponseSerializer

        a, b = V(), V()
        b.response_serializer_class = LoudResponseSerializer
        assert a.get_response_serializer_class() is EchoResponseSerializer
        assert b.get_response_serializer_class() is LoudResponseSerializer


class TestSeamOverrideResolution:
    """The whole point of the seam: a host swaps a serializer, not a method body."""

    def test_attribute_override_in_subclass(self):
        class Base(SerializerSeamMixin):
            request_serializer_class = EchoRequestSerializer
            response_serializer_class = EchoResponseSerializer

        class Host(Base):
            response_serializer_class = LoudResponseSerializer

        view = Host()
        assert view.get_response_serializer_class() is LoudResponseSerializer
        # the direction the host did NOT touch is inherited untouched
        assert view.get_request_serializer_class() is EchoRequestSerializer

    def test_getter_override_wins_over_attribute(self):
        class Base(SerializerSeamMixin):
            response_serializer_class = EchoResponseSerializer

        class Host(Base):
            def get_response_serializer_class(self):
                return LoudResponseSerializer

        assert Host().get_response_serializer_class() is LoudResponseSerializer

    def test_getter_override_may_be_per_request(self):
        class Host(SerializerSeamMixin):
            response_serializer_class = EchoResponseSerializer

            def get_response_serializer_class(self):
                if getattr(self, "premium", False):
                    return LoudResponseSerializer
                return super().get_response_serializer_class()

        view = Host()
        assert view.get_response_serializer_class() is EchoResponseSerializer
        view.premium = True
        assert view.get_response_serializer_class() is LoudResponseSerializer

    def test_instance_attribute_beats_class_attribute(self):
        class Host(SerializerSeamMixin):
            response_serializer_class = EchoResponseSerializer

        view = Host()
        view.response_serializer_class = LoudResponseSerializer
        assert view.get_response_serializer_class() is LoudResponseSerializer

    def test_mro_left_of_apiview_does_not_break_dispatch(self):
        """The seam sits first in the bases of every consumer view."""

        class V(SerializerSeamMixin, APIView):
            response_serializer_class = EchoResponseSerializer

            def get(self, request):
                cls = self.get_response_serializer_class()
                return StapelResponse(cls({"name": "ok"}))

        response = V.as_view()(factory.get("/"))
        assert response.status_code == 200
        assert response.data == {"name": "ok"}


class TestSeamDoesNotShadowDRF:
    """ViewSet modules (stapel-listings) keep their per-action seam intact."""

    def test_mixin_defines_no_get_serializer_class(self):
        assert "get_serializer_class" not in vars(SerializerSeamMixin)
        assert not hasattr(SerializerSeamMixin, "get_serializer_class")

    def test_viewset_per_action_selection_survives_the_mixin(self):
        class VS(SerializerSeamMixin, viewsets.ViewSet):
            list_serializer_class = EchoResponseSerializer
            detail_serializer_class = LoudResponseSerializer

            def get_serializer_class(self):
                if self.action == "list":
                    return self.list_serializer_class
                return self.detail_serializer_class

        vs = VS()
        vs.action = "list"
        assert vs.get_serializer_class() is EchoResponseSerializer
        vs.action = "retrieve"
        assert vs.get_serializer_class() is LoudResponseSerializer


# ---------------------------------------------------------------------------
# StapelAPIView — the thin-view contract
# ---------------------------------------------------------------------------


class TestThinViewBase:
    def test_is_an_apiview_carrying_the_seam(self):
        assert issubclass(StapelAPIView, APIView)
        assert issubclass(StapelAPIView, SerializerSeamMixin)
        assert StapelAPIView.request_serializer_class is None
        assert StapelAPIView.response_serializer_class is None

    def test_validated_request_data_returns_validated_payload(self):
        class V(StapelAPIView):
            request_serializer_class = EchoRequestSerializer

        data = V().validated_request_data(drf_post({"name": "alice"}))
        assert data["name"] == "alice"
        assert data["count"] == 1

    def test_validated_request_data_raises_drf_validation_error(self):
        class V(StapelAPIView):
            request_serializer_class = EchoRequestSerializer

        with pytest.raises(ValidationError):
            V().validated_request_data(drf_post({}))

    def test_validated_request_data_honours_partial(self):
        class V(StapelAPIView):
            request_serializer_class = EchoRequestSerializer

        data = V().validated_request_data(drf_post({"count": 3}), partial=True)
        assert data == {"count": 3}

    def test_request_serializer_goes_through_the_getter(self):
        """A host overriding only the getter changes what the helper validates."""

        class V(StapelAPIView):
            request_serializer_class = EchoRequestSerializer

        class Host(V):
            def get_request_serializer_class(self):
                class Wider(EchoRequestSerializer):
                    extra = drf_serializers.CharField(required=False, default="x")

                return Wider

        data = Host().validated_request_data(drf_post({"name": "a"}))
        assert data["extra"] == "x"

    def test_missing_request_serializer_is_a_view_bug_not_a_400(self):
        class V(StapelAPIView):
            pass

        with pytest.raises(ImproperlyConfigured) as exc:
            V().validated_request_data(drf_post({}))
        assert "request_serializer_class" in str(exc.value)

    def test_serialized_response_renders_through_the_seam(self):
        class V(StapelAPIView):
            response_serializer_class = EchoResponseSerializer

        response = V().serialized_response({"name": "alice"})
        assert isinstance(response, StapelResponse)
        assert response.status_code == 200
        assert response.data == {"name": "alice"}

    def test_serialized_response_honours_status_and_many(self):
        class V(StapelAPIView):
            response_serializer_class = EchoResponseSerializer

        response = V().serialized_response(
            [{"name": "a"}, {"name": "b"}], status=201, many=True
        )
        assert response.status_code == 201
        assert response.data == [{"name": "a"}, {"name": "b"}]

    def test_serialized_response_passes_payload_through_when_seam_is_none(self):
        """``None`` response seam = a view with no serialized body, not an error."""

        class V(StapelAPIView):
            pass

        response = V().serialized_response(None, status=204)
        assert response.status_code == 204
        assert response.data is None

    def test_response_serializer_swap_changes_the_body(self):
        class V(StapelAPIView):
            response_serializer_class = EchoResponseSerializer

        class Host(V):
            response_serializer_class = LoudResponseSerializer

        assert V().serialized_response({"name": "alice"}).data == {"name": "alice"}
        assert Host().serialized_response({"name": "alice"}).data == {"name": "ALICE"}

    def test_end_to_end_thin_view(self):
        class EchoView(StapelAPIView):
            authentication_classes = []
            permission_classes = []
            request_serializer_class = EchoRequestSerializer
            response_serializer_class = EchoResponseSerializer

            def post(self, request):
                data = self.validated_request_data(request)
                return self.serialized_response(
                    {"name": data["name"] * data["count"]}, status=201
                )

        response = EchoView.as_view()(factory.post("/", {"name": "ab", "count": 2}))
        assert response.status_code == 201
        assert response.data == {"name": "abab"}

    def test_end_to_end_host_override_needs_no_method_body(self):
        class EchoView(StapelAPIView):
            authentication_classes = []
            permission_classes = []
            request_serializer_class = EchoRequestSerializer
            response_serializer_class = EchoResponseSerializer

            def post(self, request):
                data = self.validated_request_data(request)
                return self.serialized_response({"name": data["name"]})

        class HostEchoView(EchoView):
            response_serializer_class = LoudResponseSerializer

        response = HostEchoView.as_view()(factory.post("/", {"name": "alice"}))
        assert response.status_code == 200
        assert response.data == {"name": "ALICE"}

    def test_invalid_body_answers_400_through_drf(self):
        class EchoView(StapelAPIView):
            authentication_classes = []
            permission_classes = []
            request_serializer_class = EchoRequestSerializer

            def post(self, request):
                self.validated_request_data(request)
                return StapelResponse(status=204)

        response = EchoView.as_view()(factory.post("/", {}))
        assert response.status_code == 400
