"""
Common DRF permission classes for Stapel services.

These permissions enforce staff-only access to DRF API endpoints and Swagger documentation.
"""

from rest_framework import permissions

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

