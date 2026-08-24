"""Thin-view primitives: the serializer seam, once, for the whole fleet.

The layered stance (system-design §8.1) asks a view to be *thin*: validate the
request with a serializer, hand a DTO to the service layer, render the result
through a serializer, return a :class:`~stapel_core.django.api.errors.StapelResponse`.
Nothing about that shape is module-specific — and yet every stapel module wrote
its own ``SerializerSeamMixin`` with the same two attributes and the same two
getters, because the core did not ship one (``module-extension-gaps`` §"Пробелы
из meettoday-миграции" item 2). Nineteen copies is not a pattern, it is a
missing primitive.

What the seam buys a host project
---------------------------------

A library view names its serializers as *class attributes* and reaches them
only through *getters*. A host that wants a different request shape, an extra
response field or a per-request decision subclasses the view and overrides one
attribute — it never copies an HTTP method body, and it never forks the
library::

    class MyCheckoutView(CheckoutView):
        response_serializer_class = MyCheckoutResponseSerializer

    class TenantAwareCheckoutView(CheckoutView):
        def get_response_serializer_class(self):
            return (
                PremiumCheckoutResponseSerializer
                if self.request.user.is_premium
                else super().get_response_serializer_class()
            )

``None`` is a meaningful value on either side: it says *this direction carries
no serialized payload* (a raw ``request.FILES`` upload, a 204). It is the
default so that a view declaring only one direction is not obliged to say
anything about the other.

Naming convention for views with several serializers per direction: keep the
suffix and add a purpose prefix — ``list_response_serializer_class`` with a
matching ``get_list_response_serializer_class()``. The seam is the convention,
not just these two names.

What this module deliberately does NOT do
-----------------------------------------

It does not touch DRF's own ``get_serializer_class()``. ``GenericAPIView`` and
every ``ViewSet`` already define that method, and shadowing it from a mixin
placed first in the MRO would silently disable per-action serializer selection.
A ViewSet-based module keeps its own per-action seam (stapel-listings does, on
purpose) and layers this mixin only where it drives explicit request/response
pairs.
"""
from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from rest_framework.views import APIView

from .errors import StapelResponse

__all__ = ["SerializerSeamMixin", "StapelAPIView"]


class SerializerSeamMixin:
    """Overridable request/response serializer seam for an API view.

    Declare the classes as attributes, read them through the getters::

        class WalletView(SerializerSeamMixin, APIView):
            response_serializer_class = WalletResponseSerializer

            def get(self, request):
                response_cls = self.get_response_serializer_class()
                return StapelResponse(response_cls(wallet_to_dto(...)))

    A host project swaps either direction by subclassing and setting the
    attribute, or overrides the getter for a per-request decision — no HTTP
    method body is ever copied. ``None`` means the direction carries no
    serializer (raw ``request.FILES``, an empty 204 body).
    """

    #: Serializer class used to validate incoming request data (``None``: the
    #: view reads the raw request).
    request_serializer_class = None

    #: Serializer class used to render the response body (``None``: the view
    #: returns no serialized payload).
    response_serializer_class = None

    def get_request_serializer_class(self):
        """Serializer class used to validate incoming request data."""
        return self.request_serializer_class

    def get_response_serializer_class(self):
        """Serializer class used to render the response body."""
        return self.response_serializer_class


class StapelAPIView(SerializerSeamMixin, APIView):
    """``APIView`` + the serializer seam + the two thin-view moves.

    The base a stapel module's views inherit instead of re-deriving
    ``APIView`` and a local seam mixin. The two helpers below are the only
    behaviour it adds, and both are exactly what the hand-written bodies were
    already doing, three hundred call sites over:

        def post(self, request):
            data = self.validated_request_data(request)
            dto = services.do_the_thing(**data)
            return self.serialized_response(dto, status=201)

    Neither helper is mandatory. A view with a branchy body keeps calling the
    getters directly; these exist so that the *common* body is one line per
    direction rather than three.
    """

    def get_request_serializer(self, request, *, partial=False, **kwargs):
        """Instantiate the request serializer over ``request.data``.

        Raises :class:`~django.core.exceptions.ImproperlyConfigured` when the
        view declares no request serializer — that is a bug in the view, not a
        request the client got wrong, so it must not surface as a 400.
        """
        serializer_class = self.get_request_serializer_class()
        if serializer_class is None:
            raise ImproperlyConfigured(
                f"{type(self).__name__} asked for its request serializer but "
                f"declares request_serializer_class = None. Name one, or read "
                f"request.data directly."
            )
        return serializer_class(data=request.data, partial=partial, **kwargs)

    def validated_request_data(self, request, *, partial=False, **kwargs):
        """Validated payload of the request serializer (raises DRF 400)."""
        serializer = self.get_request_serializer(request, partial=partial, **kwargs)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def serialized_response(self, payload, *, status=200, many=False, **kwargs):
        """Render *payload* through the response serializer into a response.

        With no response serializer declared the payload is passed through
        untouched — the ``None`` direction of the seam, not an error, so a
        view that answers ``204`` or a pre-rendered body still routes through
        one exit.
        """
        serializer_class = self.get_response_serializer_class()
        if serializer_class is None:
            return StapelResponse(payload, status=status)
        return StapelResponse(
            serializer_class(payload, many=many, **kwargs), status=status
        )
