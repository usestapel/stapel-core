"""
Unified error response format for all Stapel Django services.

Provides:
- StapelError dataclass for structured errors
- StapelErrorResponse helper to create DRF Response objects
- COMMON_ERRORS dict of standard error keys and templates
- Helper functions for common HTTP error responses
- ErrorKeysView base class for serving error key dictionaries
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from stapel_core.django.api.permissions import IsServiceRequest, IsStaffUser
from stapel_core.django.api.serializers import StapelDataclassSerializer

# =============================================================================
# Standard Error Key Constants
# =============================================================================

ERR_400_BAD_REQUEST = "error.400.bad_request"
ERR_401_UNAUTHORIZED = "error.401.unauthorized"
ERR_402_PAYMENT_REQUIRED = "error.402.payment_required"
ERR_403_FORBIDDEN = "error.403.forbidden"
ERR_404_NOT_FOUND = "error.404.not_found"
ERR_405_METHOD_NOT_ALLOWED = "error.405.method_not_allowed"
ERR_406_NOT_ACCEPTABLE = "error.406.not_acceptable"
ERR_408_REQUEST_TIMEOUT = "error.408.request_timeout"
ERR_409_CONFLICT = "error.409.conflict"
ERR_410_GONE = "error.410.gone"
ERR_413_PAYLOAD_TOO_LARGE = "error.413.payload_too_large"
ERR_415_UNSUPPORTED_MEDIA_TYPE = "error.415.unsupported_media_type"
ERR_422_UNPROCESSABLE_ENTITY = "error.422.unprocessable_entity"
ERR_423_LOCKED = "error.423.locked"
ERR_429_TOO_MANY_REQUESTS = "error.429.too_many_requests"
ERR_429_RATE_LIMIT = "error.429.rate_limit"
ERR_400_VALIDATION_ERROR = "error.400.validation_error"
ERR_400_EXPECTED_LIST = "error.400.expected_list"
ERR_400_INVALID_AD_ID = "error.400.invalid_ad_id"
ERR_404_AD_NOT_FOUND = "error.404.ad_not_found"
ERR_500_INTERNAL = "error.500.internal"

# =============================================================================
# Standard Error Keys
# =============================================================================

COMMON_ERRORS = {
    # 4xx Client Errors
    ERR_400_BAD_REQUEST: "Bad request",
    ERR_401_UNAUTHORIZED: "Authentication required",
    ERR_402_PAYMENT_REQUIRED: "Payment required",
    ERR_403_FORBIDDEN: "You do not have permission to perform this action",
    ERR_404_NOT_FOUND: "Requested resource not found",
    ERR_405_METHOD_NOT_ALLOWED: "Method not allowed",
    ERR_406_NOT_ACCEPTABLE: "Not acceptable",
    ERR_408_REQUEST_TIMEOUT: "Request timeout",
    ERR_409_CONFLICT: "Resource already exists",
    ERR_410_GONE: "Resource has been permanently removed",
    ERR_413_PAYLOAD_TOO_LARGE: "Request body is too large",
    ERR_415_UNSUPPORTED_MEDIA_TYPE: "Unsupported media type",
    ERR_422_UNPROCESSABLE_ENTITY: "Unprocessable entity",
    ERR_423_LOCKED: "Resource is locked",
    ERR_429_TOO_MANY_REQUESTS: "Too many requests. Please try again later.",
    ERR_429_RATE_LIMIT: "Too many attempts. Try again in {retry_after_minutes} minutes.",
    # Field-level validation (400)
    ERR_400_VALIDATION_ERROR: "Validation error",
    ERR_400_EXPECTED_LIST: "Expected a list of items",
    ERR_400_INVALID_AD_ID: "Invalid advertisement ID",
    ERR_404_AD_NOT_FOUND: "Listing not found",
    # Universal field validation — DRF error codes mapped to localizable keys.
    # Params always include {field}; some include {max_length}, {min_length}, etc.
    "error.400.field.required": "{field} is required",
    "error.400.field.null": "{field} may not be null",
    "error.400.field.blank": "{field} may not be blank",
    "error.400.field.max_length": "{field} must be at most {max_length} characters",
    "error.400.field.min_length": "{field} must be at least {min_length} characters",
    "error.400.field.max_value": "{field} must be at most {max_value}",
    "error.400.field.min_value": "{field} must be at least {min_value}",
    "error.400.field.invalid": "{field} is invalid",
    "error.400.field.invalid_choice": "{field} is not a valid choice",
    "error.400.field.does_not_exist": "{field} does not exist",
    "error.400.field.unique": "{field} must be unique",
    # 5xx Server Errors
    ERR_500_INTERNAL: "Something went wrong",
}


# =============================================================================
# Global Error Registry
# =============================================================================

_GLOBAL_REGISTRY = dict(COMMON_ERRORS)

# =============================================================================
# Remediation registry (machine-readable "what to do" hints)
# =============================================================================
#
# Each error key carries an optional *remediation* — a machine-readable hint,
# from a finite vocabulary, that tells a frontend/LLM how a user (or agent) can
# recover from the error. It is the declarative counterpart of the `en` template
# and, together with `status` + `params`, is emitted into the ``errors.json``
# codegen artifact (``generate_error_keys``) the frontend consumes.
#
# A module declares remediation alongside its keys via ``register_service_errors``
# (``remediation=`` map). It is *optional*: any key without an explicit
# declaration falls back to :func:`default_remediation`, a status+name heuristic,
# so the artifact carries a remediation for every key by construction.

#: The finite remediation vocabulary (frontend-core-architecture §2.5). A host
#: maps each to UX: retryable → "try again"; ``wait_and_retry`` → a timer from
#: ``params.retry_after_minutes``; ``verify`` → the step-up seam; ``fix_input``
#: → highlight the offending field; ``reauthenticate`` → re-login;
#: ``contact_support`` / ``bug`` → escalate.
REMEDIATION_VOCAB = frozenset(
    {
        "retry",
        "wait_and_retry",
        "reauthenticate",
        "verify",
        "fix_input",
        "contact_support",
        "bug",
    }
)

#: code -> remediation (explicit declarations only; heuristic fills the rest).
_REMEDIATION_REGISTRY: Dict[str, str] = {}


def register_service_errors(errors: dict, remediation: Optional[dict] = None):
    """Register service-specific errors into the global registry.

    ``errors`` is a ``code -> en`` map (same map the runtime ``/error-keys/``
    view serves). ``remediation`` is an optional ``code -> remediation`` map: a
    machine-readable recovery hint from :data:`REMEDIATION_VOCAB`. Every key in
    ``remediation`` must be present in ``errors`` and carry a value from the
    vocabulary; keys left undeclared fall back to :func:`default_remediation`.
    """
    _GLOBAL_REGISTRY.update(errors)
    if remediation:
        for code, hint in remediation.items():
            if code not in errors:
                raise ValueError(
                    f"remediation declared for unknown error key {code!r} "
                    f"(not in the accompanying errors map)"
                )
            if hint not in REMEDIATION_VOCAB:
                raise ValueError(
                    f"invalid remediation {hint!r} for {code!r} — "
                    f"must be one of {sorted(REMEDIATION_VOCAB)}"
                )
        _REMEDIATION_REGISTRY.update(remediation)


def _params_of(en: str) -> list:
    """`{name}` interpolation slots in a template, de-duped, first-seen order."""
    import re

    seen = []
    for m in re.finditer(r"\{(\w+)\}", en):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def default_remediation(code: str, status: int, params: list) -> str:
    """Heuristic remediation for a key with no explicit declaration.

    Ported from the frontend ``gen-errors.mjs`` provisional heuristic so an
    undeclared key resolves identically on both sides. Keyed off HTTP status and
    the key's trailing name; explicit declarations always win over this.
    """
    import re

    name = ".".join(code.split(".")[2:])
    if re.search(r"verification|step_up|enrollment", name):
        return "verify"
    if any(p.startswith("retry_after") for p in params) or status in (422, 423, 429):
        return "wait_and_retry"
    if "sso_required" in name:
        return "reauthenticate"
    if status == 401:
        return "reauthenticate"
    if status == 500:
        return "contact_support"
    if status == 409:
        return "fix_input"
    if status == 404:
        return "retry" if "not_found" in name else "fix_input"
    if status == 403:
        return "retry"
    if status == 400:
        return (
            "retry"
            if re.search(
                r"expired|challenge|not_pending|qr_(expired|fulfilled|not_found)"
                r"|magic_link_invalid|code_required",
                name,
            )
            else "fix_input"
        )
    return "retry"


def build_error_registry() -> list:
    """Project the global error registry into the ``errors.json`` structure.

    Returns a list of ``{code, status, params, remediation, en}`` dicts, sorted
    by ``code`` (byte-stable for a drift gate). ``status`` is parsed from the
    key (``error.<status>.<name>``), ``params`` from the ``en`` template, and
    ``remediation`` is the explicit declaration or :func:`default_remediation`.
    Matches the array shape the frontend ``gen-errors.mjs`` already emits, so the
    frontend can migrate onto this artifact without a format change.
    """
    entries = []
    for code, en in _GLOBAL_REGISTRY.items():
        try:
            status = int(code.split(".")[1])
        except (IndexError, ValueError):
            continue  # not an `error.<status>.<name>` key — skip defensively
        params = _params_of(en)
        remediation = _REMEDIATION_REGISTRY.get(code) or default_remediation(
            code, status, params
        )
        entries.append(
            {
                "code": code,
                "status": status,
                "params": params,
                "remediation": remediation,
                "en": en,
            }
        )
    entries.sort(key=lambda e: e["code"])
    return entries


# =============================================================================
# StapelError Dataclass & Serializer
# =============================================================================


@dataclass
class StapelError:
    """
    Structured error returned by all Stapel API endpoints.

    Attributes:
        localizable_error: Translation key for client-side localization. Example: error.404.not_found
        error: Human-readable message in English. Example: Requested resource not found
        params: Context values for template placeholders. Example: {"retry_after": 30}
    """

    localizable_error: str
    error: str
    params: Dict[str, Any] = field(default_factory=dict)


class StapelErrorSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = StapelError


# =============================================================================
# StapelResponse — required wrapper for all successful API responses
# =============================================================================


class StapelResponse(Response):
    """
    Required base class for all successful API responses in Stapel services.

    Enforces the DTO → Serializer → StapelResponse pattern and allows the static
    linter to flag bare Response() calls in view code.

    Accepts either serializer.data (a dict) or a serializer instance directly
    (auto-calls .data so the view stays one line):

        return StapelResponse(MySerializer(dto))          # preferred
        return StapelResponse(MySerializer(dto).data)     # also fine
        return StapelResponse(status=204)                 # empty response
    """

    def __init__(self, data=None, status=200, **kwargs):
        if hasattr(data, "data") and not isinstance(data, dict):
            data = data.data
        super().__init__(data=data, status=status, **kwargs)


# =============================================================================
# StapelErrorResponse Helper
# =============================================================================


def StapelErrorResponse(http_status, localizable_error, params=None):
    """
    Create a DRF Response with StapelError body.

    Error text is always looked up from the global registry.
    """
    if params is None:
        params = {}
    template = _GLOBAL_REGISTRY.get(localizable_error, localizable_error)
    try:
        error = template.format(**params)
    except (KeyError, IndexError):
        error = template

    data = StapelError(localizable_error=localizable_error, error=error, params=params)
    return Response(StapelErrorSerializer(data).data, status=http_status)


# =============================================================================
# Standard Error Helper Functions
# =============================================================================


def error_400_bad_request():
    return StapelErrorResponse(400, ERR_400_BAD_REQUEST)


def error_401_unauthorized():
    return StapelErrorResponse(401, ERR_401_UNAUTHORIZED)


def error_402_payment_required():
    return StapelErrorResponse(402, ERR_402_PAYMENT_REQUIRED)


def error_403_forbidden():
    return StapelErrorResponse(403, ERR_403_FORBIDDEN)


def error_404_not_found():
    return StapelErrorResponse(404, ERR_404_NOT_FOUND)


def error_405_method_not_allowed():
    return StapelErrorResponse(405, ERR_405_METHOD_NOT_ALLOWED)


def error_408_request_timeout():
    return StapelErrorResponse(408, ERR_408_REQUEST_TIMEOUT)


def error_409_conflict():
    return StapelErrorResponse(409, ERR_409_CONFLICT)


def error_410_gone():
    return StapelErrorResponse(410, ERR_410_GONE)


def error_413_payload_too_large():
    return StapelErrorResponse(413, ERR_413_PAYLOAD_TOO_LARGE)


def error_422_unprocessable_entity():
    return StapelErrorResponse(422, ERR_422_UNPROCESSABLE_ENTITY)


def error_423_locked():
    return StapelErrorResponse(423, ERR_423_LOCKED)


def error_429_too_many_requests():
    return StapelErrorResponse(429, ERR_429_TOO_MANY_REQUESTS)


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
    return StapelErrorResponse(
        429,
        ERR_429_RATE_LIMIT,
        params={
            "retry_after": seconds,
            "retry_after_minutes": minutes,
            "retry_after_display": format_duration(retry_after),
        },
    )


def error_500_internal():
    return StapelErrorResponse(500, ERR_500_INTERNAL)


# =============================================================================
# StapelValidationError — raise from serializers, caught by exception handler
# =============================================================================


class StapelValidationError(DRFValidationError):
    """
    Raise from serializer validators to produce StapelError responses.

    Usage:
        raise StapelValidationError(ERR_400_DISPLAY_NAME_EMOJI)
        raise StapelValidationError(ERR_400_RATE_LIMIT, params={'retry_after': 30})
    """

    def __init__(self, error_key: str, params: Optional[Dict[str, Any]] = None):
        self.error_key = error_key
        self.error_params = params or {}
        super().__init__(detail=error_key)


class StapelServiceError(Exception):
    """
    Raise from service methods to produce StapelError responses of any HTTP status.

    The exception bubbles up through DRF views and is caught by stapel_exception_handler,
    which converts it to StapelErrorResponse(http_status, error_key, params).

    Usage:
        raise StapelServiceError(429, ERR_429_RATE_LIMIT, params={'retry_after': 60})
        raise StapelServiceError(404, ERR_404_USER_FOR_RESET)
        raise StapelServiceError(422, ERR_422_BLOCKED, params={'retry_after_minutes': 5})
    """

    def __init__(
        self, http_status: int, error_key: str, params: Optional[Dict[str, Any]] = None
    ):
        self.http_status = http_status
        self.error_key = error_key
        self.error_params = params or {}
        super().__init__(error_key)


def _drf_code_to_error_key(code: str) -> str:
    """Map DRF error code to a localizable field error key."""
    mapped = f"error.400.field.{code}"
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
      StapelValidationError raised inside validate_<field>), it is preserved.
    """
    from rest_framework.exceptions import ErrorDetail

    # Simple string or ErrorDetail
    if isinstance(detail, (str, ErrorDetail)):
        key = _registered_key(detail)
        if key:
            return key, {}, str(detail)
        code = getattr(detail, "code", None)
        if code and code != "invalid":
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
        code = getattr(first, "code", None)
        if code:
            key = _drf_code_to_error_key(code)
            if key != ERR_400_VALIDATION_ERROR:
                return key, {}, str(first)
        return ERR_400_VALIDATION_ERROR, {}, str(first)

    # Dict — field-level errors, take first field's first error
    if isinstance(detail, dict):
        for field_name, errors in detail.items():
            if field_name == "non_field_errors":
                continue
            if isinstance(errors, list) and errors:
                first_err = errors[0]
            else:
                first_err = errors
            key = _registered_key(first_err)
            if key:
                return key, {"field": field_name}, str(first_err)
            code = getattr(first_err, "code", None) or "invalid"
            error_key = _drf_code_to_error_key(code)
            params = {"field": field_name}
            return error_key, params, str(first_err)

        # Only non_field_errors
        non_field = detail.get("non_field_errors", [])
        if non_field:
            first = non_field[0] if isinstance(non_field, list) else non_field
            key = _registered_key(first)
            if key:
                return key, {}, str(first)
            return ERR_400_VALIDATION_ERROR, {}, str(first)

    return ERR_400_VALIDATION_ERROR, {}, "Validation error"


def stapel_exception_handler(exc, context):
    """
    DRF exception handler — converts all validation errors to StapelError format.

    Four tiers:
    0. StapelServiceError — any HTTP status, raised from service layer
    1. StapelValidationError — business logic, has explicit error key
    2. DRF field errors — mapped via DRF error code to error.400.field.{code}
    3. Legacy/fallback — wrapped as error.400.validation_error with original message
    """
    from django.core.exceptions import ValidationError as DjangoValidationError
    from rest_framework.views import exception_handler

    # Tier 0: StapelServiceError — any HTTP status, raised from service layer
    if isinstance(exc, StapelServiceError):
        return StapelErrorResponse(exc.http_status, exc.error_key, exc.error_params)

    # Tier 1: StapelValidationError — specific business error key
    if isinstance(exc, StapelValidationError):
        return StapelErrorResponse(400, exc.error_key, exc.error_params)

    # Django model ValidationError — normalize to list/dict, then handle as DRF
    if isinstance(exc, DjangoValidationError):
        if hasattr(exc, "message_dict"):
            detail = exc.message_dict
        elif hasattr(exc, "messages"):
            detail = exc.messages
        else:
            detail = [exc.message]
        error_key, params, fallback = _extract_first_field_error(detail)
        params["detail"] = detail
        data = StapelError(localizable_error=error_key, error=fallback, params=params)
        return Response(StapelErrorSerializer(data).data, status=400)

    # Tier 2 & 3: DRF ValidationError — field errors or legacy raises
    if isinstance(exc, DRFValidationError):
        error_key, params, fallback = _extract_first_field_error(exc.detail)
        params["detail"] = exc.detail
        data = StapelError(localizable_error=error_key, error=fallback, params=params)
        return Response(StapelErrorSerializer(data).data, status=400)

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
