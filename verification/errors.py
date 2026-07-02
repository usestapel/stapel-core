"""i18n error keys of the verification mechanism."""
from stapel_core.django.api.errors import register_service_errors

from .grants import (
    ERR_400_VERIFICATION_FACTOR,
    ERR_400_VERIFICATION_FAILED,
    ERR_403_VERIFICATION_REQUIRED,
    ERR_404_VERIFICATION_CHALLENGE,
    ERR_423_VERIFICATION_LOCKED,
)

VERIFICATION_ERRORS = {
    ERR_403_VERIFICATION_REQUIRED: "Additional verification required",
    ERR_400_VERIFICATION_FACTOR: "This verification factor is not available",
    ERR_400_VERIFICATION_FAILED: "Verification failed",
    ERR_404_VERIFICATION_CHALLENGE: "Verification challenge not found or expired",
    ERR_423_VERIFICATION_LOCKED: "Too many failed attempts — verification locked",
}

register_service_errors(VERIFICATION_ERRORS)

__all__ = ["VERIFICATION_ERRORS"]
