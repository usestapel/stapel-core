"""i18n error keys of the verification mechanism."""
from stapel_core.django.api.errors import register_service_errors

from .grants import (
    ERR_400_VERIFICATION_FACTOR,
    ERR_400_VERIFICATION_FAILED,
    ERR_403_VERIFICATION_ENROLLMENT,
    ERR_403_VERIFICATION_REQUIRED,
    ERR_404_VERIFICATION_CHALLENGE,
    ERR_423_VERIFICATION_LOCKED,
)

VERIFICATION_ERRORS = {
    ERR_403_VERIFICATION_REQUIRED: "Additional verification required",
    ERR_403_VERIFICATION_ENROLLMENT: "Verification factor enrollment required",
    ERR_400_VERIFICATION_FACTOR: "This verification factor is not available",
    ERR_400_VERIFICATION_FAILED: "Verification failed",
    ERR_404_VERIFICATION_CHALLENGE: "Verification challenge not found or expired",
    ERR_423_VERIFICATION_LOCKED: "Too many failed attempts — verification locked",
}

# Machine-readable recovery hints — the step-up seam is the whole point of these
# keys, so they map to `verify` (drive the verification flow), except the lockout
# which is time-based (`wait_and_retry`). Declared explicitly rather than left to
# the heuristic: `error.404.verification_challenge_not_found` would otherwise
# resolve to `retry` (404+not_found) instead of restarting the challenge.
VERIFICATION_REMEDIATION = {
    ERR_403_VERIFICATION_REQUIRED: "verify",
    ERR_403_VERIFICATION_ENROLLMENT: "verify",
    ERR_400_VERIFICATION_FACTOR: "verify",
    ERR_400_VERIFICATION_FAILED: "verify",
    ERR_404_VERIFICATION_CHALLENGE: "verify",
    ERR_423_VERIFICATION_LOCKED: "wait_and_retry",
}

register_service_errors(VERIFICATION_ERRORS, remediation=VERIFICATION_REMEDIATION)

__all__ = ["VERIFICATION_ERRORS", "VERIFICATION_REMEDIATION"]
