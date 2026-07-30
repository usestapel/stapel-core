"""URLconf fixture for the anonymous-stance adoption check
(``stapel_core.django.adoption_checks``, E001/E002).

One view per shape the check must distinguish. The point of the fixture is
that the *green* rows outnumber the red one: the check exists to make a
decision visible, so every legitimate way of having decided has to stay
silent, otherwise it gets muted wholesale on its first real consumer.
"""
from django.urls import include, path
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    ANONYMOUS_DENIED,
    IsNotAnonymousUser,
    IsStaffUser,
)


class _Base(APIView):
    def get(self, request):  # pragma: no cover - never called
        return Response({})


# --- red: gated on bare IsAuthenticated, says nothing about guests ---------
class SilentView(_Base):
    permission_classes = [permissions.IsAuthenticated]


# --- red through inheritance: the gate lives on a project base class -------
class SilentBase(_Base):
    permission_classes = [permissions.IsAuthenticated]


class SilentChildView(SilentBase):
    """No gate of its own — the inherited one still counts."""


# --- red: declared, but with a value that is not a stance (E002) ----------
class TypoedView(_Base):
    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = "yes"


# --- green 1: guests kept out by a permission class -----------------------
class DeniedByPermissionView(_Base):
    permission_classes = [permissions.IsAuthenticated, IsNotAnonymousUser]


# --- green 2: a more specific gate (capability / role / object) -----------
class CapabilityGatedView(_Base):
    permission_classes = [permissions.IsAuthenticated, IsStaffUser]


# --- green 3: an explicit declaration on the view -------------------------
class GuestsWelcomeView(_Base):
    """The meettoday shape: an anonymous guest joining a call is the product."""

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED


class GuestsRefusedInBodyView(_Base):
    """Gate is in the view body/service layer; the header records the intent."""

    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_DENIED


class DeclaringBase(_Base):
    permission_classes = [permissions.IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED


class InheritedDeclarationView(DeclaringBase):
    """The declaration is inherited too — a base may decide for its family."""


# --- green: not a bare-IsAuthenticated gate at all ------------------------
class OpenView(_Base):
    permission_classes = [permissions.AllowAny]


class ComposedGateView(_Base):
    """DRF ``|`` composition is an OperandHolder, not IsAuthenticated."""

    permission_classes = [permissions.IsAuthenticated | IsStaffUser]


class DefaultingView(_Base):
    """Never wrote a permission_classes line — inherits the project default.

    Reported once by W001 against the setting, never here: blaming a view for
    a decision made in settings.py is how a check gets silenced.
    """


urlpatterns = [
    path("api/silent/", SilentView.as_view(), name="silent"),
    path("api/silent-child/", SilentChildView.as_view(), name="silent-child"),
    path("api/typoed/", TypoedView.as_view(), name="typoed"),
    path("api/denied/", DeniedByPermissionView.as_view(), name="denied"),
    path("api/capability/", CapabilityGatedView.as_view(), name="capability"),
    path("api/guests/", GuestsWelcomeView.as_view(), name="guests"),
    path("api/refused/", GuestsRefusedInBodyView.as_view(), name="refused"),
    path("api/inherited-decl/", InheritedDeclarationView.as_view(),
         name="inherited-decl"),
    path("api/open/", OpenView.as_view(), name="open"),
    path("api/composed/", ComposedGateView.as_view(), name="composed"),
    path("api/defaulting/", DefaultingView.as_view(), name="defaulting"),
    # Nested include — the survey must recurse into resolvers, and the same
    # view mounted twice must be reported once.
    path("nested/", include([
        path("api/silent/", SilentView.as_view(), name="nested-silent"),
    ])),
]
