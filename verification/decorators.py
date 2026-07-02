"""The @requires_verification decorator — step-up on any endpoint."""
from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)

#: Attribute the OpenAPI hook and the flows doc engine read from view
#: methods/classes to document the verification contract.
VERIFICATION_ATTR = "_stapel_verification"

TOKEN_HEADER = "X-Verification-Token"


def requires_verification(
    *,
    scope: str,
    factors: list[str] | None = None,
    max_age: int | None = None,
):
    """Reject the request with a 403 challenge envelope unless the user
    holds a fresh verification grant for *scope*.

    Apply to a DRF view method (or dispatch). The listed factors are
    interchangeable — completing any one of them creates the grant.

        @requires_verification(scope="payout",
                               factors=["otp_email", "totp", "passkey"],
                               max_age=300)
        def post(self, request): ...

    Clients: on 403 with a ``verification`` object, complete a factor via
    the auth service's verification endpoints, then retry (grant is
    server-side; stateless clients may resend the returned
    X-Verification-Token header instead).
    """

    def decorator(view_method):
        from .conf import verification_settings

        contract = {
            "scope": scope,
            "factors": factors,   # None -> resolved per-request from settings
            "max_age": max_age,
        }

        @functools.wraps(view_method)
        def wrapper(self, request, *args, **kwargs):
            from .grants import create_challenge, has_grant, verification_error_payload

            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated:
                # Step-up presumes an authenticated user; leave 401 handling
                # to the view's permission classes.
                return view_method(self, request, *args, **kwargs)

            token = request.headers.get(TOKEN_HEADER) or None
            if has_grant(user, scope, token=token):
                return view_method(self, request, *args, **kwargs)

            effective_factors = list(
                factors or verification_settings.DEFAULT_FACTORS
            )
            effective_max_age = int(max_age or verification_settings.DEFAULT_MAX_AGE)
            challenge = create_challenge(user, scope, effective_factors, effective_max_age)
            logger.info(
                "verification required user=%s scope=%s challenge=%s",
                user.pk, scope, challenge["challenge_id"],
            )
            return _forbidden(verification_error_payload(challenge))

        # Annotate both the wrapper (read via the class attribute lookup)
        # and keep the contract for OpenAPI/flow docs.
        setattr(wrapper, VERIFICATION_ATTR, contract)
        return wrapper

    return decorator


def _forbidden(payload: dict):
    from rest_framework import status
    from rest_framework.response import Response

    return Response(payload, status=status.HTTP_403_FORBIDDEN)


def view_verification_contract(view_cls) -> dict | None:
    """The verification contract of a view class (any annotated method)."""
    direct = getattr(view_cls, VERIFICATION_ATTR, None)
    if direct:
        return direct
    for name in ("get", "post", "put", "patch", "delete"):
        method = getattr(view_cls, name, None)
        contract = getattr(method, VERIFICATION_ATTR, None)
        if contract:
            return contract
    return None
