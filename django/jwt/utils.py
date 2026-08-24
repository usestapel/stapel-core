"""
Django utility functions for JWT authentication.

Provides helper functions for loading user data from Django User model.
"""

import logging
from typing import Any, Dict, Optional

from django.db import IntegrityError, transaction

logger = logging.getLogger(__name__)


def load_jwt_config_from_settings():
    """
    Load JWT configuration from Django settings.

    This function creates a JWTConfig instance with all necessary parameters
    from Django settings, supporting both HS256 and RS256 algorithms.

    For RS256, keys can be provided either:
    - Directly as PEM content via JWT_PRIVATE_KEY/JWT_PUBLIC_KEY settings
    - As file paths via JWT_PRIVATE_KEY_PATH/JWT_PUBLIC_KEY_PATH settings

    Returns:
        JWTConfig instance configured from Django settings
    """
    from datetime import timedelta

    from django.conf import settings

    from stapel_core.core.config import JWTConfig

    algorithm = getattr(settings, "JWT_ALGORITHM", "HS256")

    issuer = getattr(settings, "JWT_ISSUER", "stapel-auth")
    # JWKS URL for jku header - derive from issuer if not explicitly set
    jwks_url = getattr(settings, "JWT_JWKS_URL", None)
    if not jwks_url and issuer.startswith("http"):
        prefix = getattr(settings, "STAPEL_AUTH_SERVICE_PREFIX", "auth") or ""
        prefix_part = f"/{prefix}" if prefix else ""
        jwks_url = f"{issuer}{prefix_part}/.well-known/jwks.json"

    config_params = {
        "algorithm": algorithm,
        "access_token_lifetime": timedelta(
            seconds=getattr(settings, "JWT_ACCESS_TOKEN_LIFETIME", 3600)
        ),
        "refresh_token_lifetime": timedelta(
            seconds=getattr(settings, "JWT_REFRESH_TOKEN_LIFETIME", 604800)
        ),
        "issuer": issuer,
        "audience": getattr(
            settings, "JWT_AUDIENCE", None
        ),  # None = don't verify audience
        "jwks_url": jwks_url,
        # Cookie settings - must match between set_jwt_cookies and delete_cookie
        "cookie_name": getattr(settings, "JWT_COOKIE_NAME", "stapel_jwt"),
        "refresh_cookie_name": getattr(
            settings, "JWT_REFRESH_COOKIE_NAME", "stapel_refresh_jwt"
        ),
        "cookie_domain": getattr(settings, "JWT_COOKIE_DOMAIN", None),
        # Default True: a service that never declared the setting still
        # gets a TLS-only cookie. Opting out is an explicit False.
        "cookie_secure": getattr(settings, "JWT_COOKIE_SECURE", True),
        "cookie_httponly": getattr(settings, "JWT_COOKIE_HTTPONLY", True),
        "cookie_samesite": getattr(settings, "JWT_COOKIE_SAMESITE", "Lax"),
    }

    if algorithm == "RS256":
        # For RS256, prefer direct key content over file paths
        private_key = getattr(settings, "JWT_PRIVATE_KEY", None)
        public_key = getattr(settings, "JWT_PUBLIC_KEY", None)

        if private_key:
            config_params["private_key"] = private_key
        elif getattr(settings, "JWT_PRIVATE_KEY_PATH", None):
            config_params["private_key_path"] = settings.JWT_PRIVATE_KEY_PATH

        if public_key:
            config_params["public_key"] = public_key
        elif getattr(settings, "JWT_PUBLIC_KEY_PATH", None):
            config_params["public_key_path"] = settings.JWT_PUBLIC_KEY_PATH

        config_params["secret_key"] = ""  # Not used for RS256
    else:
        # For HS256, use secret_key
        secret = getattr(settings, "JWT_SECRET_KEY", settings.SECRET_KEY)
        # A well-known default secret means anyone can mint superuser tokens.
        # Refuse to start outside DEBUG rather than run forgeable.
        if not getattr(settings, "DEBUG", False) and (
            not secret or secret.startswith("django-insecure-")
        ):
            from django.core.exceptions import ImproperlyConfigured

            raise ImproperlyConfigured(
                "JWT is configured for HS256 with a missing or default secret. "
                "Set JWT_SECRET_KEY (or SECRET_KEY) to a strong value, or "
                "provide JWT_PRIVATE_KEY/JWT_PUBLIC_KEY for RS256."
            )
        config_params["secret_key"] = secret

    return JWTConfig(**config_params)


def _get_user_model():
    """
    Lazy import of User model to avoid ImproperlyConfigured errors.

    Django's get_user_model() should only be called after apps are loaded.
    """
    from django.contrib.auth import get_user_model

    return get_user_model()


def _create_users_from_token() -> bool:
    """Whether a bearer token may create local users and rewrite their flags.

    ``True`` is consumer (shadow-copy) mode: an unknown ``user_id`` is
    materialised as a local row, and ``is_staff`` / ``is_superuser`` /
    ``is_active`` are REPLACED from the token's claims on every request. It is
    the right mode for a downstream service whose users genuinely live in the
    auth service — and it means one compromised signing key, or one
    over-broad claim, mints a local superuser.

    ``False`` is authoritative mode: the local database decides who exists and
    what they may do, and a token that names an unknown user is treated as
    stale.

    The default used to be ``True``, so a service that never thought about the
    question got the trusting mode. It is ``False`` now: consuming an external
    identity source is a design decision a service makes on purpose, and the
    setting is where it says so.
    """
    from django.conf import settings

    return bool(getattr(settings, "JWT_CREATE_USERS_FROM_TOKEN", False))


def load_user_by_uid(uid: str) -> Optional[Dict[str, Any]]:
    """
    Load fresh user data from the database by primary key.

    This is the **re-mint loader**: ``TokenManager.refresh_access_token``
    calls it so that a new access token carries what the database says now,
    not what the presented refresh token claimed when it was issued (up to
    ``JWT_REFRESH_TOKEN_LIFETIME``, 7 days by default). Every refresh path in
    the package passes it — the middleware's two, and the explicit
    ``JWTRefreshView``.

    Returning ``None`` is how a refresh is REFUSED, so this function decides
    who may still be re-credentialled, and it refuses two cases:

    * the user no longer exists — a deleted account cannot be re-minted;
    * the user is no longer active — deactivation ("close this account",
      "suspend this employee") must take effect on the next refresh, not up
      to a week later. Without this, a refresh token outlived the account it
      spoke for.

    Args:
        uid: User primary key, as carried in the token's ``user_id`` claim

    Returns:
        Dictionary with fresh user data, or None if the user is missing,
        deactivated, or unreadable
    """
    User = _get_user_model()
    try:
        user = User.objects.get(pk=uid)
    except User.DoesNotExist:
        logger.warning(f"User not found: {uid}")
        return None
    except Exception as e:
        logger.error(f"Error loading user by uid: {e}")
        return None

    if not getattr(user, "is_active", True):
        logger.warning(f"Refusing to re-mint for deactivated user: {uid}")
        return None

    try:
        return serialize_user_to_jwt_data(user)
    except Exception as e:
        logger.error(f"Error loading user by uid: {e}")
        return None


def serialize_user_to_jwt_data(user) -> Dict[str, Any]:
    """
    Convert Django User model to JWT payload data.

    Args:
        user: Django User instance

    Returns:
        Dictionary with user data for JWT token
    """
    data = {
        "user_id": str(user.pk),
        "email": user.email,
        "username": user.username,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "is_active": user.is_active,
    }

    # Add optional fields if they exist on the user model
    if hasattr(user, "is_anonymous"):
        data["is_anonymous"] = user.is_anonymous
    if hasattr(user, "auth_type"):
        data["auth_type"] = user.auth_type
    if hasattr(user, "phone") and user.phone:
        data["phone"] = user.phone

    # Staff roles claim (admin-suite AS-2): staff/superuser tokens only.
    # Present-but-empty is authoritative ("zero roles"); absence means the
    # user model has no field (pre-AS-2) — consumers must not touch local
    # state for such tokens.
    if data["is_staff"] or data["is_superuser"]:
        roles = getattr(user, "staff_roles", None)
        if roles is not None:
            data["staff_roles"] = sorted(str(r) for r in roles)

    return data


def _apply_jwt_fields(user, user_data: Dict[str, Any], phone=None):
    """Apply optional JWT fields (is_anonymous, auth_type, phone) to user."""
    if hasattr(user, "is_anonymous") and "is_anonymous" in user_data:
        user.is_anonymous = user_data["is_anonymous"]
    if hasattr(user, "auth_type") and "auth_type" in user_data:
        user.auth_type = user_data["auth_type"]
    if phone and hasattr(user, "phone"):
        user.phone = phone


def _ensure_user_in_staff_group(user) -> bool:
    """
    Add user to Staff group if not already a member.

    Args:
        user: Django User instance

    Returns:
        True if user was added, False if already member or error
    """
    try:
        from stapel_core.django.groups import add_user_to_staff_group

        return add_user_to_staff_group(user)
    except Exception as e:
        logger.warning(f"Could not add user to Staff group: {e}")
        return False


def get_or_create_user_from_jwt(user_data: Dict[str, Any]):
    """
    Get or create the Django user a validated token speaks for.

    This is the one seam every JWT authentication path in the package shares
    — the middleware, the DRF authentication class, ``JWTAuthBackend`` and
    the channels middleware all resolve their principal through it, and all
    four already treat ``None`` as "authenticate nobody". So this is where
    the account-lifecycle gate belongs, not repeated at four call sites where
    the fifth caller written next month would forget it.

    **An inactive user is nobody.** Django's own contract is that
    ``is_active=False`` cannot authenticate (``ModelBackend`` enforces it via
    ``user_can_authenticate``), but ``django.contrib.auth.login()`` does not
    check — and the JWT paths call ``login()`` directly, having verified a
    signature rather than a password. Without this gate a closed or suspended
    account kept authenticating on every one of them until its access token
    expired on its own.

    The check runs AFTER the sync below, deliberately: in consumer mode the
    ``is_active`` claim is replayed onto the local row first, so a user this
    service had stale-disabled is reactivated by the authoritative claim and
    then passes. In authoritative mode the local database wins and a closed
    account is refused, which is the whole point of closing it.

    Args:
        user_data: User data from a token that has ALREADY been validated

    Returns:
        Django User instance, or None if the user could not be resolved or
        is not active
    """
    user = _get_or_create_user_from_jwt(user_data)

    if user is not None and not getattr(user, "is_active", True):
        logger.warning(
            "JWT authentication refused: user %s is not active",
            user_data.get("user_id"),
        )
        return None

    # Bridge to stapel_core.access (AS-1): stamp the validated claim onto the
    # request user instance so MandateBackend's claim_roles source reads the
    # FRESH token, not a stale stored field. Transient attribute,
    # request-scoped (CLAIM_ATTR = "_stapel_staff_roles_claim").
    if user is not None and "staff_roles" in user_data:
        from stapel_core.access.sources import CLAIM_ATTR

        setattr(
            user, CLAIM_ATTR, [str(r) for r in (user_data.get("staff_roles") or [])]
        )
    return user


def _get_or_create_user_from_jwt(user_data: Dict[str, Any]):
    """Core get-or-create logic for :func:`get_or_create_user_from_jwt`.

    The public wrapper stamps the transient ``staff_roles`` claim
    (CLAIM_ATTR) onto whatever user this returns.
    """
    User = _get_user_model()
    pk = user_data.get("user_id")
    if not pk:
        logger.error("No id in JWT data")
        return None

    try:
        # Try to get existing user by PK
        user = User.objects.get(pk=pk)

        # Staff status sync-down (admin-suite AS-2, c.3).
        updated = False
        create_from_jwt = _create_users_from_token()

        if create_from_jwt:
            # Consumer (shadow-copy) mode — REPLACE from the claim (c.3):
            # auth is the source of truth for staff status. The old
            # "upgrade-only" rule is gone: it made revocation impossible (A3)
            # AND let a replayed stale token re-elevate a demoted admin.
            jwt_is_staff = bool(user_data.get("is_staff", False))
            jwt_is_superuser = bool(user_data.get("is_superuser", False))
            if user.is_staff != jwt_is_staff:
                user.is_staff = jwt_is_staff
                updated = True
            if user.is_superuser != jwt_is_superuser:
                user.is_superuser = jwt_is_superuser
                updated = True
            # Roles: REPLACE only when the claim is present. Absence =
            # pre-AS-2 token = no information: never grant and never revoke
            # from silence (no downgrade AND no upgrade by an old token).
            if "staff_roles" in user_data and hasattr(user, "staff_roles"):
                claim_roles = [str(r) for r in (user_data.get("staff_roles") or [])]
                if list(user.staff_roles or []) != claim_roles:
                    user.staff_roles = claim_roles
                    updated = True
            # Account lifecycle, same rule as roles: replace only when the
            # claim is present. Absence = no information — a token minted
            # before the claim existed must not activate anybody.
            if "is_active" in user_data:
                jwt_is_active = bool(user_data["is_active"])
                if user.is_active != jwt_is_active:
                    user.is_active = jwt_is_active
                    updated = True
        # else: authoritative-user-store mode (auth service / monolith with
        # stapel-auth): the local DB is canonical, a token must never write
        # staff attributes OR account lifecycle back into it. (This fixes the
        # pre-AS-2 hole where a stale staff token replayed at the auth service
        # re-elevated a demoted admin via upgrade-only, and audit GDPR-01,
        # where a token issued before account closure — which carries
        # is_active=true for the rest of its lifetime — wrote that value back
        # and undid the closure. Closure and reactivation belong to the
        # lifecycle service, never to a bearer token.)

        # Email: replace only when the claim carries one. Writing the absent
        # case through nulled the column on every request that presented a
        # token without it.
        jwt_email = user_data.get("email")
        if jwt_email and user.email != jwt_email:
            user.email = jwt_email
            updated = True

        # Sync is_anonymous, auth_type, phone from JWT
        if hasattr(user, "is_anonymous") and "is_anonymous" in user_data:
            if user.is_anonymous != user_data["is_anonymous"]:
                user.is_anonymous = user_data["is_anonymous"]
                updated = True
        if hasattr(user, "auth_type") and "auth_type" in user_data:
            if user.auth_type != user_data["auth_type"]:
                user.auth_type = user_data["auth_type"]
                updated = True
        jwt_phone = user_data.get("phone") or None
        if jwt_phone and hasattr(user, "phone") and user.phone != jwt_phone:
            user.phone = jwt_phone
            updated = True

        if updated:
            user.save()
            logger.info(f"Updated user from JWT: {pk}")

        # Auto-add staff users to Staff group
        if user.is_staff and not user.is_superuser:
            _ensure_user_in_staff_group(user)

        return user

    except User.DoesNotExist:
        # Check if we should create users from JWT
        # Auth service should NOT create users - if user_id not found, JWT is stale
        # Other services should create users to sync from auth service
        create_from_jwt = _create_users_from_token()

        if not create_from_jwt:
            # Auth service mode: reject stale JWT, user must re-login
            logger.warning(
                f"User {pk} not found and JWT_CREATE_USERS_FROM_TOKEN=False. JWT is stale."
            )
            return None

        # Microservice mode: create user from JWT
        try:
            username = user_data.get("username")
            email = user_data.get("email")

            # Normalize empty strings to None to avoid unique constraint issues
            # Empty strings violate unique constraints, but NULLs are allowed
            if email == "":
                email = None

            phone = user_data.get("phone")
            if phone == "":
                phone = None
            elif phone:
                # Normalize phone to E.164
                try:
                    import phonenumbers

                    parsed = phonenumbers.parse(phone, None)
                    if phonenumbers.is_valid_number(parsed):
                        phone = phonenumbers.format_number(
                            parsed, phonenumbers.PhoneNumberFormat.E164
                        )
                except Exception:
                    pass

            # Ensure username is never None - generate if needed
            if not username:
                import uuid

                username = f"user_{uuid.uuid4().hex[:8]}"

            # Try to find existing user by phone/email/username (in case PK changed after DB reset)
            # Phone is most reliable for phone-based auth
            existing_user = None
            if phone:
                existing_user = User.objects.filter(phone=phone).first()
            if not existing_user and email:
                existing_user = User.objects.filter(email=email).first()
            if not existing_user and username:
                existing_user = User.objects.filter(username=username).first()

            if existing_user:
                # Update existing user's PK to match JWT
                # This handles DB reset scenarios where same user has different PK
                logger.info(
                    f"Found existing user by email/phone/username, updating PK from {existing_user.pk} to {pk}"
                )
                old_pk = existing_user.pk
                if old_pk != pk:
                    # Atomic delete+create to prevent race conditions
                    with transaction.atomic():
                        User.objects.filter(pk=old_pk).delete()
                        user = User.objects.create_user(
                            pk=pk,
                            email=email,
                            username=username,
                            is_staff=user_data.get("is_staff", False),
                            is_superuser=user_data.get("is_superuser", False),
                            is_active=user_data.get("is_active", True),
                        )
                    _apply_jwt_fields(user, user_data, phone)
                    if "staff_roles" in user_data and hasattr(user, "staff_roles"):
                        user.staff_roles = [
                            str(r) for r in (user_data.get("staff_roles") or [])
                        ]
                    user.set_unusable_password()
                    user.save()
                    logger.info(f"Re-created user with new PK: {pk}")
                    return user
                return existing_user

            try:
                with transaction.atomic():
                    user = User.objects.create_user(
                        pk=pk,
                        email=email,
                        username=username,
                        is_staff=user_data.get("is_staff", False),
                        is_superuser=user_data.get("is_superuser", False),
                        is_active=user_data.get("is_active", True),
                    )
            except IntegrityError:
                # Race condition: another request created the same user
                user = User.objects.filter(pk=pk).first()
                if user:
                    return user
                raise

            _apply_jwt_fields(user, user_data, phone)
            if "staff_roles" in user_data and hasattr(user, "staff_roles"):
                user.staff_roles = [
                    str(r) for r in (user_data.get("staff_roles") or [])
                ]
            # Set unusable password since auth is handled by auth service
            user.set_unusable_password()
            user.save()

            # Auto-add staff users to Staff group
            if user.is_staff and not user.is_superuser:
                _ensure_user_in_staff_group(user)

            return user
        except Exception as e:
            logger.error(f"Error creating user: {e}", exc_info=True)
            return None
    except Exception as e:
        logger.error(f"Error getting/creating user: {e}", exc_info=True)
        return None


def extract_jwt_from_request(request) -> tuple[Optional[str], Optional[str]]:
    """
    Extract JWT tokens from Django request.

    Checks both cookies and Authorization header.

    Args:
        request: Django HttpRequest instance

    Returns:
        Tuple of (access_token, refresh_token)
    """
    from django.conf import settings

    # Get cookie names from settings or use defaults
    cookie_name = getattr(settings, "JWT_COOKIE_NAME", "stapel_jwt")
    refresh_cookie_name = getattr(
        settings, "JWT_REFRESH_COOKIE_NAME", "stapel_refresh_jwt"
    )

    # Try to get from cookies first
    access_token = request.COOKIES.get(cookie_name)
    refresh_token = request.COOKIES.get(refresh_cookie_name)

    # Fallback to Authorization header for access token
    if not access_token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            access_token = auth_header[7:]

    return access_token, refresh_token


def set_jwt_cookies(response, access_token: str, refresh_token: Optional[str] = None):
    """
    Set JWT tokens as HTTP-only cookies on response.

    Args:
        response: Django HttpResponse instance
        access_token: JWT access token
        refresh_token: Optional JWT refresh token
    """
    from django.conf import settings

    # Get settings or use defaults
    cookie_name = getattr(settings, "JWT_COOKIE_NAME", "stapel_jwt")
    refresh_cookie_name = getattr(
        settings, "JWT_REFRESH_COOKIE_NAME", "stapel_refresh_jwt"
    )
    cookie_domain = getattr(settings, "JWT_COOKIE_DOMAIN", None)
    # Default True, matching get_jwt_config: absent setting means TLS-only.
    cookie_secure = getattr(settings, "JWT_COOKIE_SECURE", True)
    cookie_httponly = getattr(settings, "JWT_COOKIE_HTTPONLY", True)
    cookie_samesite = getattr(settings, "JWT_COOKIE_SAMESITE", "Lax")
    access_token_lifetime = getattr(settings, "JWT_ACCESS_TOKEN_LIFETIME", 3600)
    refresh_token_lifetime = getattr(settings, "JWT_REFRESH_TOKEN_LIFETIME", 604800)

    # Set access token cookie
    response.set_cookie(
        cookie_name,
        access_token,
        max_age=access_token_lifetime,
        domain=cookie_domain,
        path="/",
        secure=cookie_secure,
        httponly=cookie_httponly,
        samesite=cookie_samesite,
    )

    # Set refresh token cookie if provided
    if refresh_token:
        response.set_cookie(
            refresh_cookie_name,
            refresh_token,
            max_age=refresh_token_lifetime,
            domain=cookie_domain,
            path="/",
            secure=cookie_secure,
            httponly=cookie_httponly,
            samesite=cookie_samesite,
        )


def setup_centralized_admin_login(admin_site, auth_service_prefix: str = "auth"):
    """
    Configure Django admin to redirect login to centralized auth service.

    This function monkey-patches the admin site's login method to redirect
    unauthenticated users to the centralized auth service instead of showing
    a local login page.

    Args:
        admin_site: Django AdminSite instance (usually django.contrib.admin.site)
        auth_service_prefix: URL prefix for auth service (default: 'auth')

    Example:
        from django.contrib import admin
        from stapel_core.django.jwt.utils import setup_centralized_admin_login

        # In urls.py, before defining urlpatterns
        setup_centralized_admin_login(admin.site, auth_service_prefix='auth')
    """
    from urllib.parse import urlencode

    from django.conf import settings
    from django.shortcuts import redirect
    from django.urls import get_script_prefix

    # Get current service URL prefix
    url_prefix = getattr(settings, "URL_PREFIX", "")

    def custom_admin_login(request, extra_context=None):
        """Redirect to centralized auth service for login."""
        # Get the page the user was trying to access
        # If 'next' parameter exists, use it; otherwise default to current service admin
        # (script-prefix aware — survives sub-path deployments).
        root = get_script_prefix()
        next_url = request.GET.get("next", f"{root}{url_prefix}admin/")

        # Build login URL with next parameter
        login_url = f"{root}{auth_service_prefix}/admin/login/"
        if next_url and next_url != login_url:
            login_url = f"{login_url}?{urlencode({'next': next_url})}"

        return redirect(login_url)

    # Monkey-patch the admin site login method
    admin_site.login = custom_admin_login


def get_admin_logout_urlpattern(
    url_prefix: str = "", auth_service_prefix: str = "auth"
):
    """
    Get URL pattern for JWT-based admin logout.

    This returns a URL pattern that MUST be added BEFORE admin.site.urls
    to properly override Django's default logout.

    Args:
        url_prefix: URL prefix for current service (e.g., 'translate/')
        auth_service_prefix: URL prefix for auth service (default: 'auth')

    Returns:
        URL pattern for admin logout

    Example:
        from stapel_core.django.jwt.utils import get_admin_logout_urlpattern

        urlpatterns = [
            # Logout MUST be before admin.site.urls
            get_admin_logout_urlpattern(url_prefix, 'auth'),
            path(f'{url_prefix}admin/', admin.site.urls),
        ]
    """
    from django.conf import settings
    from django.shortcuts import redirect
    from django.urls import get_script_prefix, path

    from stapel_core.django.jwt.views import JWTLogoutView

    class AdminJWTLogoutView(JWTLogoutView):
        """Logout view that redirects to auth service login page."""

        def _do_logout_and_redirect(self, request):
            """Perform logout and return redirect with cleared cookies."""
            request._jwt_skip_cookie_update = True
            super().post(request)

            redirect_response = redirect(
                f"{get_script_prefix()}{auth_service_prefix}/admin/login/"
            )

            cookie_name = getattr(settings, "JWT_COOKIE_NAME", "stapel_jwt")
            refresh_cookie_name = getattr(
                settings, "JWT_REFRESH_COOKIE_NAME", "stapel_refresh_jwt"
            )
            cookie_domain = getattr(settings, "JWT_COOKIE_DOMAIN", None)
            cookie_samesite = getattr(settings, "JWT_COOKIE_SAMESITE", "Lax")

            redirect_response.delete_cookie(
                cookie_name, path="/", domain=cookie_domain, samesite=cookie_samesite
            )
            redirect_response.delete_cookie(
                refresh_cookie_name,
                path="/",
                domain=cookie_domain,
                samesite=cookie_samesite,
            )
            return redirect_response

        def post(self, request):
            return self._do_logout_and_redirect(request)

        def get(self, request):
            return self._do_logout_and_redirect(request)

    return path(
        f"{url_prefix}admin/logout/", AdminJWTLogoutView.as_view(), name="admin-logout"
    )


def reset_sequences_for_models(*models):
    """
    Reset PostgreSQL sequences for specified models.

    Call this after bulk operations that insert data with explicit IDs
    (fixtures, imports, migrations) to ensure sequences are in sync.

    Args:
        *models: Django model classes to reset sequences for.
                 If no models provided, resets all models in the project.

    Example:
        from categories.models import Feature, Category
        from stapel_core.django.jwt.utils import reset_sequences_for_models

        # After bulk import
        Feature.objects.bulk_create(features_with_ids)
        reset_sequences_for_models(Feature)

        # Or reset multiple models
        reset_sequences_for_models(Feature, Category)

        # Or reset all models (slower, use sparingly)
        reset_sequences_for_models()
    """
    from django.db import connection

    from django.apps import apps

    if models:
        model_list = models
    else:
        model_list = apps.get_models()

    for model in model_list:
        pk_field = model._meta.pk
        if pk_field is None:
            continue

        field_type = type(pk_field).__name__
        if field_type not in ("AutoField", "BigAutoField"):
            continue

        table_name = model._meta.db_table
        pk_column = pk_field.column
        sequence_name = f"{table_name}_{pk_column}_seq"

        try:
            with connection.cursor() as cursor:
                # Get max ID
                cursor.execute(f'SELECT MAX("{pk_column}") FROM "{table_name}"')
                row = cursor.fetchone()
                max_id = row[0] if row else 0

                if max_id is None:
                    max_id = 0

                # Get current sequence value
                cursor.execute(f'SELECT last_value FROM "{sequence_name}"')
                row = cursor.fetchone()
                current_val = row[0] if row else 0

                # Reset if needed
                if current_val < max_id:
                    cursor.execute(
                        f"SELECT setval('\"{sequence_name}\"', %s)", [max_id]
                    )
                    logger.info(
                        f"Reset sequence {sequence_name}: {current_val} -> {max_id}"
                    )
        except Exception as e:
            logger.warning(f"Could not reset sequence for {table_name}: {e}")
