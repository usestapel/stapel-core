"""
Unified error response format for all Iron Django services.

Provides:
- IronError dataclass for structured errors
- IronErrorResponse helper to create DRF Response objects
- COMMON_ERRORS dict of standard error keys and templates
- Helper functions for common HTTP error responses
- ErrorKeysView base class for serving error key dictionaries
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from stapel_core.django.api.serializers import IronDataclassSerializer
from stapel_core.django.api.permissions import IsServiceRequest, IsStaffUser


# =============================================================================
# Standard Error Key Constants
# =============================================================================

ERR_400_BAD_REQUEST = 'error.400.bad_request'
ERR_401_UNAUTHORIZED = 'error.401.unauthorized'
ERR_402_PAYMENT_REQUIRED = 'error.402.payment_required'
ERR_403_FORBIDDEN = 'error.403.forbidden'
ERR_404_NOT_FOUND = 'error.404.not_found'
ERR_405_METHOD_NOT_ALLOWED = 'error.405.method_not_allowed'
ERR_406_NOT_ACCEPTABLE = 'error.406.not_acceptable'
ERR_408_REQUEST_TIMEOUT = 'error.408.request_timeout'
ERR_409_CONFLICT = 'error.409.conflict'
ERR_410_GONE = 'error.410.gone'
ERR_413_PAYLOAD_TOO_LARGE = 'error.413.payload_too_large'
ERR_415_UNSUPPORTED_MEDIA_TYPE = 'error.415.unsupported_media_type'
ERR_422_UNPROCESSABLE_ENTITY = 'error.422.unprocessable_entity'
ERR_423_LOCKED = 'error.423.locked'
ERR_429_TOO_MANY_REQUESTS = 'error.429.too_many_requests'
ERR_429_RATE_LIMIT = 'error.429.rate_limit'
ERR_400_VALIDATION_ERROR = 'error.400.validation_error'
ERR_400_EXPECTED_LIST = 'error.400.expected_list'
ERR_400_INVALID_AD_ID = 'error.400.invalid_ad_id'
ERR_404_AD_NOT_FOUND = 'error.404.ad_not_found'
ERR_500_INTERNAL = 'error.500.internal'

# =============================================================================
# Standard Error Keys
# =============================================================================

COMMON_ERRORS = {
    # 4xx Client Errors
    ERR_400_BAD_REQUEST: 'Bad request',
    ERR_401_UNAUTHORIZED: 'Authentication required',
    ERR_402_PAYMENT_REQUIRED: 'Payment required',
    ERR_403_FORBIDDEN: 'You do not have permission to perform this action',
    ERR_404_NOT_FOUND: 'Requested resource not found',
    ERR_405_METHOD_NOT_ALLOWED: 'Method not allowed',
    ERR_406_NOT_ACCEPTABLE: 'Not acceptable',
    ERR_408_REQUEST_TIMEOUT: 'Request timeout',
    ERR_409_CONFLICT: 'Resource already exists',
    ERR_410_GONE: 'Resource has been permanently removed',
    ERR_413_PAYLOAD_TOO_LARGE: 'Request body is too large',
    ERR_415_UNSUPPORTED_MEDIA_TYPE: 'Unsupported media type',
    ERR_422_UNPROCESSABLE_ENTITY: 'Unprocessable entity',
    ERR_423_LOCKED: 'Resource is locked',
    ERR_429_TOO_MANY_REQUESTS: 'Too many requests. Please try again later.',
    ERR_429_RATE_LIMIT: 'Too many attempts. Try again in {retry_after_minutes} minutes.',

    # Field-level validation (400)
    ERR_400_VALIDATION_ERROR: 'Validation error',
    ERR_400_EXPECTED_LIST: 'Expected a list of items',
    ERR_400_INVALID_AD_ID: 'Invalid advertisement ID',
    ERR_404_AD_NOT_FOUND: 'Listing not found',

    # Universal field validation — DRF error codes mapped to localizable keys.
    # Params always include {field}; some include {max_length}, {min_length}, etc.
    'error.400.field.required': '{field} is required',
    'error.400.field.null': '{field} may not be null',
    'error.400.field.blank': '{field} may not be blank',
    'error.400.field.max_length': '{field} must be at most {max_length} characters',
    'error.400.field.min_length': '{field} must be at least {min_length} characters',
    'error.400.field.max_value': '{field} must be at most {max_value}',
    'error.400.field.min_value': '{field} must be at least {min_value}',
    'error.400.field.invalid': '{field} is invalid',
    'error.400.field.invalid_choice': '{field} is not a valid choice',
    'error.400.field.does_not_exist': '{field} does not exist',
    'error.400.field.unique': '{field} must be unique',

    # 5xx Server Errors
    ERR_500_INTERNAL: 'Something went wrong',
}


# =============================================================================
# Global Error Registry
# =============================================================================

_GLOBAL_REGISTRY = dict(COMMON_ERRORS)


def register_service_errors(errors: dict):
    """Register service-specific errors into the global registry."""
    _GLOBAL_REGISTRY.update(errors)


# =============================================================================
# IronError Dataclass & Serializer
# =============================================================================

@dataclass
class IronError:
    """
    Structured error returned by all Iron API endpoints.

    Attributes:
        localizable_error: Translation key for client-side localization. Example: error.404.not_found
        error: Human-readable message in English. Example: Requested resource not found
        params: Context values for template placeholders. Example: {"retry_after": 30}
    """
    localizable_error: str
    error: str
    params: Dict[str, Any] = field(default_factory=dict)


class IronErrorSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = IronError


# =============================================================================
# IronResponse — required wrapper for all successful API responses
# =============================================================================

class IronResponse(Response):
    """
    Required base class for all successful API responses in Iron services.

    Enforces the DTO → Serializer → IronResponse pattern and allows the static
    linter to flag bare Response() calls in view code.

    Accepts either serializer.data (a dict) or a serializer instance directly
    (auto-calls .data so the view stays one line):

        return IronResponse(MySerializer(dto))          # preferred
        return IronResponse(MySerializer(dto).data)     # also fine
        return IronResponse(status=204)                 # empty response
    """

    def __init__(self, data=None, status=200, **kwargs):
        if hasattr(data, 'data') and not isinstance(data, dict):
            data = data.data
        super().__init__(data=data, status=status, **kwargs)


# =============================================================================
# IronErrorResponse Helper
# =============================================================================

def IronErrorResponse(http_status, localizable_error, params=None):
    """
    Create a DRF Response with IronError body.

    Error text is always looked up from the global registry.
    """
    if params is None:
        params = {}
    template = _GLOBAL_REGISTRY.get(localizable_error, localizable_error)
    try:
        error = template.format(**params)
    except (KeyError, IndexError):
        error = template

    data = IronError(localizable_error=localizable_error, error=error, params=params)
    return Response(IronErrorSerializer(data).data, status=http_status)


# =============================================================================
# Standard Error Helper Functions
# =============================================================================

def error_400_bad_request():
    return IronErrorResponse(400, ERR_400_BAD_REQUEST)


def error_401_unauthorized():
    return IronErrorResponse(401, ERR_401_UNAUTHORIZED)


def error_402_payment_required():
    return IronErrorResponse(402, ERR_402_PAYMENT_REQUIRED)


def error_403_forbidden():
    return IronErrorResponse(403, ERR_403_FORBIDDEN)


def error_404_not_found():
    return IronErrorResponse(404, ERR_404_NOT_FOUND)


def error_405_method_not_allowed():
    return IronErrorResponse(405, ERR_405_METHOD_NOT_ALLOWED)


def error_408_request_timeout():
    return IronErrorResponse(408, ERR_408_REQUEST_TIMEOUT)


def error_409_conflict():
    return IronErrorResponse(409, ERR_409_CONFLICT)


def error_410_gone():
    return IronErrorResponse(410, ERR_410_GONE)


def error_413_payload_too_large():
    return IronErrorResponse(413, ERR_413_PAYLOAD_TOO_LARGE)


def error_422_unprocessable_entity():
    return IronErrorResponse(422, ERR_422_UNPROCESSABLE_ENTITY)


def error_423_locked():
    return IronErrorResponse(423, ERR_423_LOCKED)


def error_429_too_many_requests():
    return IronErrorResponse(429, ERR_429_TOO_MANY_REQUESTS)


def format_duration(seconds):
    """Format seconds as M:SS or H:MM:SS (for >= 3600s)."""
    seconds = int(seconds or 0)
    if seconds >= 3600:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def error_429_rate_limit(retry_after):
    """Rate limit error with formatted retry_after display."""
    import math
    seconds = int(retry_after or 0)
    minutes = max(1, math.ceil(seconds / 60))
    return IronErrorResponse(429, ERR_429_RATE_LIMIT, params={
        'retry_after': seconds,
        'retry_after_minutes': minutes,
        'retry_after_display': format_duration(retry_after),
    })


def error_500_internal():
    return IronErrorResponse(500, ERR_500_INTERNAL)


# =============================================================================
# IronValidationError — raise from serializers, caught by exception handler
# =============================================================================

class IronValidationError(DRFValidationError):
    """
    Raise from serializer validators to produce IronError responses.

    Usage:
        raise IronValidationError(ERR_400_DISPLAY_NAME_EMOJI)
        raise IronValidationError(ERR_400_RATE_LIMIT, params={'retry_after': 30})
    """
    def __init__(self, error_key: str, params: Optional[Dict[str, Any]] = None):
        self.error_key = error_key
        self.error_params = params or {}
        super().__init__(detail=error_key)


class IronServiceError(Exception):
    """
    Raise from service methods to produce IronError responses of any HTTP status.

    The exception bubbles up through DRF views and is caught by iron_exception_handler,
    which converts it to IronErrorResponse(http_status, error_key, params).

    Usage:
        raise IronServiceError(429, ERR_429_RATE_LIMIT, params={'retry_after': 60})
        raise IronServiceError(404, ERR_404_USER_FOR_RESET)
        raise IronServiceError(422, ERR_422_BLOCKED, params={'retry_after_minutes': 5})
    """
    def __init__(self, http_status: int, error_key: str, params: Optional[Dict[str, Any]] = None):
        self.http_status = http_status
        self.error_key = error_key
        self.error_params = params or {}
        super().__init__(error_key)


def _drf_code_to_error_key(code: str) -> str:
    """Map DRF error code to a localizable field error key."""
    mapped = f'error.400.field.{code}'
    if mapped in _GLOBAL_REGISTRY:
        return mapped
    return ERR_400_VALIDATION_ERROR


def _registered_key(value) -> Optional[str]:
    """If the value (or its string form) is a registered error key, return it."""
    s = str(value) if value is not None else None
    if s and s in _GLOBAL_REGISTRY:
        return s
    return None


def _extract_first_field_error(detail):
    """
    Extract the first field error from DRF ValidationError detail.

    Returns (error_key, params, fallback_message).
    - For field errors: error_key='error.400.field.required', params={'field': 'code'}
    - For non-field: error_key='error.400.validation_error', params={}
    - If the detail string itself is a registered error key (e.g. an
      IronValidationError raised inside validate_<field>), it is preserved.
    """
    from rest_framework.exceptions import ErrorDetail

    # Simple string or ErrorDetail
    if isinstance(detail, (str, ErrorDetail)):
        key = _registered_key(detail)
        if key:
            return key, {}, str(detail)
        code = getattr(detail, 'code', None)
        if code and code != 'invalid':
            key = _drf_code_to_error_key(code)
            if key != ERR_400_VALIDATION_ERROR:
                return key, {}, str(detail)
        return ERR_400_VALIDATION_ERROR, {}, str(detail)

    # List — non-field errors, take first
    if isinstance(detail, list) and detail:
        first = detail[0]
        key = _registered_key(first)
        if key:
            return key, {}, str(first)
        code = getattr(first, 'code', None)
        if code:
            key = _drf_code_to_error_key(code)
            if key != ERR_400_VALIDATION_ERROR:
                return key, {}, str(first)
        return ERR_400_VALIDATION_ERROR, {}, str(first)

    # Dict — field-level errors, take first field's first error
    if isinstance(detail, dict):
        for field_name, errors in detail.items():
            if field_name == 'non_field_errors':
                continue
            if isinstance(errors, list) and errors:
                first_err = errors[0]
            else:
                first_err = errors
            key = _registered_key(first_err)
            if key:
                return key, {'field': field_name}, str(first_err)
            code = getattr(first_err, 'code', None) or 'invalid'
            error_key = _drf_code_to_error_key(code)
            params = {'field': field_name}
            return error_key, params, str(first_err)

        # Only non_field_errors
        non_field = detail.get('non_field_errors', [])
        if non_field:
            first = non_field[0] if isinstance(non_field, list) else non_field
            key = _registered_key(first)
            if key:
                return key, {}, str(first)
            return ERR_400_VALIDATION_ERROR, {}, str(first)

    return ERR_400_VALIDATION_ERROR, {}, 'Validation error'


def iron_exception_handler(exc, context):
    """
    DRF exception handler — converts all validation errors to IronError format.

    Three tiers:
    1. IronValidationError — business logic, has explicit error key
    2. DRF field errors — mapped via DRF error code to error.400.field.{code}
    3. Legacy/fallback — wrapped as error.400.validation_error with original message
    """
    from django.core.exceptions import ValidationError as DjangoValidationError
    from rest_framework.views import exception_handler

    # Tier 0: IronServiceError — any HTTP status, raised from service layer
    if isinstance(exc, IronServiceError):
        return IronErrorResponse(exc.http_status, exc.error_key, exc.error_params)

    # Tier 1: IronValidationError — specific business error key
    if isinstance(exc, IronValidationError):
        return IronErrorResponse(400, exc.error_key, exc.error_params)

    # Django model ValidationError — normalize to list/dict, then handle as DRF
    if isinstance(exc, DjangoValidationError):
        if hasattr(exc, 'message_dict'):
            detail = exc.message_dict
        elif hasattr(exc, 'messages'):
            detail = exc.messages
        else:
            detail = [exc.message]
        error_key, params, fallback = _extract_first_field_error(detail)
        params['detail'] = detail
        data = IronError(localizable_error=error_key, error=fallback, params=params)
        return Response(IronErrorSerializer(data).data, status=400)

    # Tier 2 & 3: DRF ValidationError — field errors or legacy raises
    if isinstance(exc, DRFValidationError):
        error_key, params, fallback = _extract_first_field_error(exc.detail)
        params['detail'] = exc.detail
        data = IronError(localizable_error=error_key, error=fallback, params=params)
        return Response(IronErrorSerializer(data).data, status=400)

    return exception_handler(exc, context)


# =============================================================================
# ErrorKeysView
# =============================================================================

class ErrorKeysView(APIView):
    """
    GET /{prefix}/api/error-keys/

    Returns the full dict of error keys and their English templates.
    Services subclass this and override get_service_errors() to add custom keys.
    """
    permission_classes = [IsServiceRequest | IsStaffUser]

    def get_service_errors(self):
        return {}

    schema = None  # Exclude from OpenAPI schema

    def get(self, request):
        return Response(_GLOBAL_REGISTRY)
