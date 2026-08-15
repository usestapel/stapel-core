"""
Common DRF permission classes for Stapel services.

These permissions enforce staff-only access to DRF API endpoints and Swagger documentation.
"""

from rest_framework import exceptions, permissions

#: Attribute a view sets to state, in its own source, what an *anonymous*
#: (guest) session may do with it — the declaration the ``stapel_adoption``
#: E001 check asks for when the ``AUTH_ANONYMOUS`` axis is on and the view's
#: only gate is a bare ``IsAuthenticated`` (which a guest session passes).
#:
#: Deliberately a ``stapel_``-prefixed name with a *closed* value vocabulary:
#: nothing in Django or DRF carries it, so it cannot appear by accident, and a
#: typo in the value is reported (E002) instead of silently reading as
#: "declared".
ANONYMOUS_DECLARATION_ATTR = "stapel_anonymous_access"

#: ``stapel_anonymous_access = ANONYMOUS_ALLOWED`` — guests are *meant* to
#: reach this view (a guest joining a call, a public read). The check goes
#: quiet; the intent is now readable in the view instead of implied by the
#: absence of a permission class.
ANONYMOUS_ALLOWED = "anonymous-allowed"

#: ``stapel_anonymous_access = ANONYMOUS_DENIED`` — guests must not reach this
#: view, and the gate that keeps them out lives elsewhere than
#: ``permission_classes`` (a service-layer check, an object permission, a
#: mixin). Prefer adding :class:`IsNotAnonymousUser` when the gate *can* be a
#: permission class: the declaration records an intent, the permission class
#: enforces it.
ANONYMOUS_DENIED = "anonymous-denied"

#: The only two admissible values of :data:`ANONYMOUS_DECLARATION_ATTR`.
ANONYMOUS_DECLARATIONS = (ANONYMOUS_ALLOWED, ANONYMOUS_DENIED)


class IsStaffUser(permissions.BasePermission):
    """
    Permission class that only allows staff users to access the API.

    This ensures that DRF browsable API and Swagger documentation
    are only accessible to authenticated staff users (those logged into admin).

    Usage:
        In settings.py:
        REST_FRAMEWORK = {
            'DEFAULT_PERMISSION_CLASSES': [
                'stapel_core.django.api.permissions.IsStaffUser',
            ],
        }

        Or in individual views:
        class MyViewSet(viewsets.ModelViewSet):
            permission_classes = [IsStaffUser]
    """

    def has_permission(self, request, view):
        """
        Check if user is authenticated and is staff.

        Args:
            request: Django request object
            view: DRF view object

        Returns:
            bool: True if user is staff, False otherwise
        """
        return bool(
            request.user and
            request.user.is_authenticated and
            (request.user.is_staff or request.user.is_superuser)
        )


class IsSuperUser(permissions.BasePermission):
    """
    Permission class that only allows superusers to access the API.

    Usage:
        class AdminOnlyViewSet(viewsets.ModelViewSet):
            permission_classes = [IsSuperUser]
    """

    def has_permission(self, request, view):
        """
        Check if user is authenticated and is superuser.

        Args:
            request: Django request object
            view: DRF view object

        Returns:
            bool: True if user is superuser, False otherwise
        """
        return bool(
            request.user and
            request.user.is_authenticated and
            request.user.is_superuser
        )


class ReadOnlyOrSuperUser(permissions.BasePermission):
    """
    Allow reading for everyone, but only superuser can alter.

    Usage:
        class CategoryViewSet(viewsets.ModelViewSet):
            permission_classes = [ReadOnlyOrSuperUser]
    """

    def has_permission(self, request, view):
        """
        Allow read-only for anyone, modify only for superusers.

        Args:
            request: Django request object
            view: DRF view object

        Returns:
            bool: True if read-only or superuser
        """
        # Allow read-only access for everyone (including anonymous)
        if request.method in permissions.SAFE_METHODS:
            return True
        # For write operations, require authenticated superuser
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.is_superuser


class ReadOnlyOrStaff(permissions.BasePermission):
    """
    Allow reading for everyone, but only staff users can alter.

    Usage:
        class CategoryViewSet(viewsets.ModelViewSet):
            permission_classes = [ReadOnlyOrStaff]
    """

    def has_permission(self, request, view):
        """
        Allow read-only for anyone, modify only for staff users.

        Args:
            request: Django request object
            view: DRF view object

        Returns:
            bool: True if read-only or staff user
        """
        # Allow read-only access for everyone (including anonymous)
        if request.method in permissions.SAFE_METHODS:
            return True
        # For write operations, require authenticated staff user
        if not request.user or not request.user.is_authenticated:
            return False
        return request.user.is_staff or request.user.is_superuser


class IsServiceRequest(permissions.BasePermission):
    """
    Allows access to requests that were marked as internal service calls
    by ServiceAPIKeyMiddleware (X-API-KEY).
    """

    def has_permission(self, request, view):
        return bool(getattr(request, "is_service_request", False))


class IsNotAnonymousUser(permissions.BasePermission):
    """
    Permission that requires authenticated non-anonymous user.

    Anonymous users (is_anonymous=True) are rejected even if authenticated.
    Use this for operations that require a real user account (e.g., posting ads).

    Usage:
        class AdCreateView(APIView):
            permission_classes = [IsNotAnonymousUser]
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        # Check if user is anonymous (has is_anonymous attribute and it's True)
        if getattr(request.user, 'is_anonymous', False):
            return False
        return True


class MandateUnavailable(exceptions.APIException):
    """503 — the mandate question could not be asked.

    Not 403. "You hold no mandate" is a verdict about the user; "workspaces is
    unreachable" is a fact about the deployment, and rendering the second as
    the first is how a routing skew once told a workspace's own owner they
    were Forbidden. The status separates them for the client, the logs and the
    operator alike.
    """

    status_code = 503
    default_detail = "Cannot verify workspace mandate right now."
    #: Spelled out rather than imported: ``api.errors`` imports this module, so
    #: the arrow cannot point back. ``ERR_503_MANDATE_UNAVAILABLE`` is the
    #: registered half, and tests/test_mandate.py pins the two together.
    default_code = "error.503.mandate_unavailable"


class HasWorkspaceMandate(permissions.BasePermission):
    """Requires an ACTIVE MANDATE, not merely a real account.

    The third principal state, enforced. ``IsAuthenticated`` admits anyone
    with a session; :class:`IsNotAnonymousUser` admits anyone with a real
    account — including a registered user who belongs to no workspace at all,
    which is precisely the guest state
    (``stapel_workspaces.permissions.is_guest``). This class is the only one
    of the three that asks whether the caller holds a mandate ANYWHERE.

    Deliberately a new name rather than a widened ``IsNotAnonymousUser``: a
    class that reads as "is a real user" must not quietly start meaning "holds
    a mandate", and the confusion between those two readings is what made an
    earlier fix miss. Both classes stay, and they mean different things.

    Three answers, one refusal:

    * anonymous → ``False`` (403, or 401 with an authentication class);
    * mandate-less (guest) → ``False`` (403);
    * mandated → ``True``;
    * *could not ask* → raises :class:`MandateUnavailable` (503). An
      authorization question with no answer degrades to refusal, and says so
      honestly instead of impersonating a verdict.

    Per-workspace authority is a different question with a different answer:
    use ``stapel_core.django.workspaces.require_capability`` for "may X act in
    workspace W". This one is the coarse door — "is this person part of any
    organization at all" — which is what a chat operator flag, an unconditional
    ``can()`` or a by-UUID read actually needed and never asked.

    Usage::

        class TaskListView(APIView):
            permission_classes = [HasWorkspaceMandate]
    """

    #: The gate is explicit, so the adoption check has its answer already.
    stapel_anonymous_access = ANONYMOUS_DENIED

    def has_permission(self, request, view):
        from stapel_core.django.mandate import (
            MandateLookupUnavailable,
            MandateState,
            mandate_state,
        )

        try:
            state = mandate_state(getattr(request, "user", None))
        except MandateLookupUnavailable as exc:
            raise MandateUnavailable() from exc
        return state is MandateState.MANDATED


class HasWorkspaceMandateIfScoped(permissions.BasePermission):
    """:class:`HasWorkspaceMandate` for a view a SINGLE-TENANT host also runs.

    Same three answers, one difference: a deployment where nothing can answer
    the mandate question — no ``workspaces.check_mandate`` route, no
    ``stapel_workspaces`` in the process — has no mandates for anyone to hold,
    so the guest state does not exist in it and this gate admits. The strict
    class refuses there instead (503), and ``stapel_core.mandate.E001`` calls
    that out at ``manage.py check``; correct for a product view whose author
    knows the deployment, wrong for a library view that ships to both shapes.

    Which to pick:

    * a PRODUCT view, in a service you know embeds or routes to workspaces →
      :class:`HasWorkspaceMandate`. Fail closed; E001 tells you if you wired
      it wrong, before the first 503.
    * a LIBRARY view (stapel-calendar, stapel-chat, …) that a single-tenant
      host also mounts → this one. The single-tenant host keeps working; the
      multi-tenant host gets the third state enforced, and the module's own
      ``SCOPE_PROVIDER`` check errors if it is also still running a
      single-scope provider.

    "Standalone" is settings-and-registry only, never a liveness probe: a
    seam that IS wired and then fails to answer still raises
    :class:`MandateUnavailable` (503). Unreachable-by-configuration and
    unreachable-right-now are different facts and only the first is a
    deployment shape.
    """

    #: The gate is explicit, so the adoption check has its answer already.
    stapel_anonymous_access = ANONYMOUS_DENIED

    def has_permission(self, request, view):
        from stapel_core.django.scope import deployment_is_standalone

        user = getattr(request, "user", None)
        # An anonymous session is refused in every deployment shape: it is
        # the one state that needs no lookup to recognise.
        if not getattr(user, "is_authenticated", False) or getattr(
            user, "is_anonymous", False
        ):
            return False
        if deployment_is_standalone():
            return True
        return HasWorkspaceMandate().has_permission(request, view)

