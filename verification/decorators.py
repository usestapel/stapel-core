"""The @requires_verification decorator — step-up on any endpoint."""
from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)

#: Attribute the OpenAPI hook and the flows doc engine read from view
#: methods/classes to document the verification contract.
VERIFICATION_ATTR = "_stapel_verification"

TOKEN_HEADER = "X-Verification-Token"

#: Policy levels, from non-negotiable to user-elected.
VERIFICATION_LEVELS = ("strict", "default_on", "opt_in")


def requires_verification(
    *,
    scope: str,
    factors: list[str] | None = None,
    max_age: int | None = None,
    level: str | None = "strict",
):
    """Reject the request with a 403 challenge envelope unless the user
    holds a fresh verification grant for *scope*.

    Apply to a DRF view method (or dispatch). The listed factors are
    interchangeable — completing any one of them creates the grant.

        @requires_verification(scope="payout",
                               factors=["otp_email", "totp", "passkey"],
                               max_age=300)
        def post(self, request): ...

    ``level`` sets the enforcement policy:

    - ``"strict"`` (default): always enforced, the user cannot opt out. A
      user with no usable factor gets a 403 ENROLLMENT envelope
      (``verification.enroll = true``, no challenge is stored) telling the
      client to enroll one of the endpoint's factors first.
    - ``"default_on"``: enforced when the user has at least one usable
      factor AND has not disabled *scope* in their verification
      preferences; otherwise the request passes through to the view.
    - ``"opt_in"``: enforced only when the user explicitly enabled *scope*
      (and has a usable factor); otherwise the request passes through.

    Pass ``level=None`` to defer to ``STAPEL_VERIFICATION["DEFAULT_LEVEL"]``.
    Preferences are resolved via the ``auth.verification.policy`` comm
    Function (see .policy) and FAIL SAFE when it is unavailable:
    ``default_on`` protection stays on, ``opt_in`` stays off.

    Clients: on 403 with a ``verification`` object, complete a factor via
    the auth service's verification endpoints, then retry (grant is
    server-side; stateless clients may resend the returned
    X-Verification-Token header instead).
    """
    if level is not None and level not in VERIFICATION_LEVELS:
        raise ValueError(
            f"unknown verification level {level!r} "
            f"(expected one of {', '.join(VERIFICATION_LEVELS)}, or None)"
        )

    def decorator(view_method):
        from .conf import verification_settings

        contract = {
            "scope": scope,
            "factors": factors,   # None -> resolved per-request from settings
            "max_age": max_age,
            "level": level,       # None -> resolved per-request from settings
        }

        @functools.wraps(view_method)
        def wrapper(self, request, *args, **kwargs):
            from .factors import factor_registry
            from .grants import (
                create_challenge,
                has_grant,
                verification_enrollment_payload,
                verification_error_payload,
            )
            from .policy import scope_enforced

            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated:
                # Step-up presumes an authenticated user; leave 401 handling
                # to the view's permission classes.
                return view_method(self, request, *args, **kwargs)

            token = request.headers.get(TOKEN_HEADER) or None
            if has_grant(user, scope, token=token):
                return view_method(self, request, *args, **kwargs)

            effective_level = level or str(verification_settings.DEFAULT_LEVEL)
            if effective_level not in VERIFICATION_LEVELS:
                effective_level = "strict"  # misconfigured default: fail safe

            effective_factors = list(
                factors or verification_settings.DEFAULT_FACTORS
            )
            usable = factor_registry.available_for(user, effective_factors)

            if effective_level == "strict":
                if not usable:
                    # Nothing the user could complete — nothing to verify
                    # yet, so no challenge is stored; the client must take
                    # the user through factor enrollment first.
                    logger.info(
                        "verification enrollment required user=%s scope=%s",
                        user.pk, scope,
                    )
                    return _forbidden(
                        verification_enrollment_payload(scope, effective_factors)
                    )
            else:
                if not usable or not scope_enforced(user, scope, effective_level):
                    return view_method(self, request, *args, **kwargs)

            effective_max_age = int(max_age or verification_settings.DEFAULT_MAX_AGE)
            challenge = create_challenge(user, scope, effective_factors, effective_max_age)
            logger.info(
                "verification required user=%s scope=%s level=%s challenge=%s",
                user.pk, scope, effective_level, challenge["challenge_id"],
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
