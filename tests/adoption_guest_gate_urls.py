"""URLconf fixture for the runtime guest probe (``adoption_checks`` W003).

Every view here carries a gate that static inspection reads as "a position
was taken" — more than a bare ``IsAuthenticated``, so E001 is silent about
all of them. Running the gate is what tells them apart, and the fixture is
built so that the silent rows outnumber the reported one: a probe that
reported more than the genuinely ambiguous shape would be muted on its first
consumer.
"""
from django.urls import path
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    IsNotAnonymousUser,
)


class _Base(APIView):
    def get(self, request):  # pragma: no cover - never called
        return Response({})


class NotClosing(permissions.BasePermission):
    """A business gate: it asks whether the account is being erased.

    Orthogonal to identity, and a guest passes it. This is the shape that
    made five user-facing views in a library invisible to E001 while they
    admitted guest sessions.
    """

    def has_permission(self, request, view):
        return not getattr(request.user, "closing", False)


class NobodyPasses(permissions.BasePermission):
    def has_permission(self, request, view):
        return False


class NeedsTheDatabase(permissions.BasePermission):
    """A gate that cannot be evaluated by a probe — no verdict, no finding."""

    def has_permission(self, request, view):
        raise RuntimeError("this gate reads a row the probe has not created")


# --- red: refuses an unauthenticated caller, admits a guest, says nothing --
class BusinessGatedView(_Base):
    permission_classes = [permissions.IsAuthenticated, NotClosing]


class ComposedGateView(_Base):
    """``|`` is an OperandHolder, so E001 reads it as a position taken.

    A guest still passes the left operand.
    """

    permission_classes = [permissions.IsAuthenticated | NobodyPasses]


# --- green: the second class does ask about identity ----------------------
class IdentityGatedView(_Base):
    permission_classes = [permissions.IsAuthenticated, IsNotAnonymousUser]


# --- green: refuses everyone, guests included -----------------------------
class RefusesBothView(_Base):
    permission_classes = [permissions.IsAuthenticated, NobodyPasses]


# --- green: public — admits an unauthenticated caller too ------------------
class PublicView(_Base):
    permission_classes = [permissions.AllowAny]


# --- green: the ambiguity is real and the view says so --------------------
class DeclaredBusinessGatedView(_Base):
    permission_classes = [permissions.IsAuthenticated, NotClosing]
    stapel_anonymous_access = ANONYMOUS_ALLOWED


class DeclaringBase(_Base):
    permission_classes = [permissions.IsAuthenticated, NotClosing]
    stapel_anonymous_access = ANONYMOUS_ALLOWED


class InheritedDeclarationView(DeclaringBase):
    """The stance is looked up through the MRO, as E001 looks it up."""


# --- green: no verdict, because the gate cannot be run --------------------
class UnprobeableView(_Base):
    permission_classes = [permissions.IsAuthenticated, NeedsTheDatabase]


urlpatterns = [
    path("api/business/", BusinessGatedView.as_view(), name="business"),
    path("api/composed/", ComposedGateView.as_view(), name="composed"),
    path("api/identity/", IdentityGatedView.as_view(), name="identity"),
    path("api/refuses-both/", RefusesBothView.as_view(), name="refuses-both"),
    path("api/public/", PublicView.as_view(), name="public"),
    path("api/declared/", DeclaredBusinessGatedView.as_view(), name="declared"),
    path("api/inherited-decl/", InheritedDeclarationView.as_view(),
         name="inherited-decl"),
    path("api/unprobeable/", UnprobeableView.as_view(), name="unprobeable"),
]
