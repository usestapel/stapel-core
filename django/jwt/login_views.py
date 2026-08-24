"""
Django admin cookie-login view.

This module owns exactly one credential-processing path in the core: the
browser form at ``admin/login.html`` that exchanges a username and password
for a Django session **and** a fleet-wide JWT cookie pair. Everything a JWT
grants anywhere in the deployment is granted here, so this is the one place
in the package where "who may present credentials" and "who may receive
tokens" have to be the same question.

The answer is **staff only**, and it is not configurable. This view IS the
admin login view — its template is the admin's, its redirect target is the
admin index, its `dispatch()` already refuses to keep a non-staff session.
A deployment that legitimately needs a non-admin cookie login needs a
different view, not a flag that loosens this one: a setting that can turn a
staff gate off is a staff gate that is off in whichever environment nobody
audited.
"""

import logging
from django.contrib.auth.views import LoginView
from django.contrib.auth import login
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from .utils import set_jwt_cookies
from .provider import jwt_provider

logger = logging.getLogger(__name__)

# Deliberately word-for-word Django's own admin refusal (see
# django.contrib.admin.forms.AdminAuthenticationForm.error_messages). A
# distinct "you are not staff" message would confirm to an attacker that the
# password they just tried is the right one for that account.
NON_STAFF_LOGIN_ERROR = _(
    "Please enter the correct username and password for a staff account. "
    "Note that both fields may be case-sensitive."
)


def has_admin_access(user) -> bool:
    """Is this user allowed to hold an admin session and admin JWT cookies?

    Read defensively with ``getattr`` for the same reason ``dispatch()``
    does: a swapped user model, an anonymous user and a test double all reach
    this function, and a missing attribute must read as "no", never raise.
    """
    return bool(
        getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
    )


class JWTCookieLoginView(LoginView):
    """
    Admin login view that also issues the JWT cookie pair.

    Two independent staff gates, on purpose:

    1. ``get_form_class()`` resolves to Django's ``AdminAuthenticationForm``,
       which fails validation for a non-staff account, so a non-staff
       credential never reaches ``form_valid()`` at all.
    2. ``form_valid()`` refuses a non-staff user again, before ``login()``
       and before any token is minted.

    (1) alone is not enough: a subclass that names its own
    ``authentication_form`` — a perfectly ordinary thing to do, to add a
    captcha or a "remember me" field — would silently reopen a full
    authentication bypass. (2) alone is not enough either: it would let the
    plain form report "logged in" and then strand the user. So both.
    """

    template_name = 'admin/login.html'

    # Resolved lazily in get_form_class(), NOT assigned here: importing
    # django.contrib.admin.forms at module-import time pulls in
    # django.contrib.auth.forms -> the User model, which raises
    # AppRegistryNotReady for any project that imports this module from a
    # urls.py or settings module loaded before django.setup(). Same lazy
    # pattern the rest of this file uses for redirect/logout/mounts.
    authentication_form = None

    def get_form_class(self):
        """Django's admin login form unless a subclass named its own.

        A subclass that sets ``authentication_form`` still wins — it may have
        good reasons — and ``form_valid()`` holds the line regardless.
        """
        if self.authentication_form is not None:
            return self.authentication_form

        from django.contrib.admin.forms import AdminAuthenticationForm

        return AdminAuthenticationForm

    def dispatch(self, request, *args, **kwargs):
        """
        Redirect if user is already authenticated AND has admin access.
        Shows login form if user is not staff (to allow switching accounts).
        """
        from django.shortcuts import redirect

        if request.user.is_authenticated:
            # Only redirect if user has admin access (is_staff or is_superuser)
            if has_admin_access(request.user):
                next_url = request.GET.get('next', '')
                # Prevent redirect loop - if next is login page, go to admin index
                if not next_url or '/login' in next_url:
                    # Deployment-canonical admin index (mount/script-prefix
                    # aware) instead of a hardcoded root-relative path.
                    from stapel_core.django.mounts import admin_index_url
                    next_url = admin_index_url()
                logger.info(f"Staff user {request.user} redirecting to {next_url}")
                return redirect(next_url)
            else:
                # User is authenticated but not staff - clear JWT cookies and session
                from django.contrib.auth import logout
                logger.info(f"Non-staff user {request.user}, clearing auth and showing login form")
                logout(request)
                # Clear JWT cookies by returning response with deleted cookies
                response = super().dispatch(request, *args, **kwargs)
                return self._clear_jwt_cookies(response)

        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _clear_jwt_cookies(response):
        """Delete both auth cookies with the deployment's cookie attributes."""
        cookie_name = getattr(settings, 'JWT_COOKIE_NAME', 'stapel_jwt')
        refresh_cookie_name = getattr(settings, 'JWT_REFRESH_COOKIE_NAME', 'stapel_refresh_jwt')
        cookie_domain = getattr(settings, 'JWT_COOKIE_DOMAIN', None)
        cookie_samesite = getattr(settings, 'JWT_COOKIE_SAMESITE', 'Lax')
        response.delete_cookie(cookie_name, path='/', domain=cookie_domain, samesite=cookie_samesite)
        response.delete_cookie(refresh_cookie_name, path='/', domain=cookie_domain, samesite=cookie_samesite)
        return response

    def form_valid(self, form):
        """
        Handle valid form submission.

        A correct username and password is proof of identity, not a grant of
        admin access. Staff is checked BEFORE ``login()`` and before
        ``create_tokens()``, so a non-staff account that types its own correct
        password leaves with nothing: no session, no access token, no refresh
        token, no cookies.

        Args:
            form: Django authentication form

        Returns:
            HTTP response with JWT cookies set, or the re-rendered form
        """
        # Get authenticated user
        user = form.get_user()

        if not has_admin_access(user):
            return self._refuse_non_staff(form, user)

        # Log the user in (establishes Django session)
        login(self.request, user, backend='django.contrib.auth.backends.ModelBackend')

        try:
            # Use unified JWT provider for token generation
            access_token, refresh_token = jwt_provider.create_tokens(user)

            response = super().form_valid(form)
            set_jwt_cookies(response, access_token, refresh_token)

            return response

        except Exception as e:
            logger.error(f"Error generating JWT tokens: {e}", exc_info=True)
            return super().form_valid(form)

    def _refuse_non_staff(self, form, user):
        """Re-render the login form; issue nothing.

        Deliberately the same shape as a wrong password (the form, again,
        with the admin's own wording) rather than a 403: the response must
        not tell an attacker that the credential was valid. Any stale auth
        cookies on the request are cleared on the way out, so a refused
        attempt cannot leave a half-authenticated browser behind.
        """
        from django.core.exceptions import ValidationError

        logger.warning(
            "Refused admin cookie login for non-staff user %r: no session, no tokens",
            getattr(user, "pk", None),
        )
        form.add_error(None, ValidationError(NON_STAFF_LOGIN_ERROR, code="invalid_login"))
        return self._clear_jwt_cookies(self.form_invalid(form))
