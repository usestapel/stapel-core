"""Convenience re-exports from stapel_core.django.api.errors.

Explicit re-exports (rather than ``import *``) so the shim is robust against
partial-module circular imports: a wildcard captures only the names that exist
in ``api.errors`` at the moment the shim is first imported, which can be a
partially-populated module during app loading.
"""
from stapel_core.django.api.errors import (  # noqa: F401
    COMMON_ERRORS,
    ErrorKeysView,
    # Error key constants
    ERR_400_BAD_REQUEST,
    ERR_400_EXPECTED_LIST,
    ERR_400_VALIDATION_ERROR,
    ERR_400_INVALID_AD_ID,
    ERR_401_UNAUTHORIZED,
    ERR_402_PAYMENT_REQUIRED,
    ERR_403_FORBIDDEN,
    ERR_404_AD_NOT_FOUND,
    ERR_404_NOT_FOUND,
    ERR_405_METHOD_NOT_ALLOWED,
    ERR_406_NOT_ACCEPTABLE,
    ERR_408_REQUEST_TIMEOUT,
    ERR_409_CONFLICT,
    ERR_410_GONE,
    ERR_413_PAYLOAD_TOO_LARGE,
    ERR_415_UNSUPPORTED_MEDIA_TYPE,
    ERR_422_UNPROCESSABLE_ENTITY,
    ERR_423_LOCKED,
    ERR_429_TOO_MANY_REQUESTS,
    ERR_429_RATE_LIMIT,
    ERR_500_INTERNAL,
    # Canonical names
    StapelError,
    StapelErrorSerializer,
    StapelErrorResponse,
    StapelResponse,
    StapelServiceError,
    StapelValidationError,
    # Helper functions
    error_400_bad_request,
    error_401_unauthorized,
    error_402_payment_required,
    error_403_forbidden,
    error_404_not_found,
    error_405_method_not_allowed,
    error_408_request_timeout,
    error_409_conflict,
    error_410_gone,
    error_413_payload_too_large,
    error_422_unprocessable_entity,
    error_423_locked,
    error_429_too_many_requests,
    error_429_rate_limit,
    error_500_internal,
    format_duration,
    register_service_errors,
    stapel_exception_handler,
)
