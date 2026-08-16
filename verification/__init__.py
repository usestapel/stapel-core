"""stapel_core.verification — step-up verification on any endpoint.

Attach an OTP/TOTP/passkey requirement to any view without baking factor
logic into it:

    from stapel_core.verification import requires_verification

    class PayoutView(APIView):
        @requires_verification(scope="payout",
                               factors=["otp_email", "totp", "passkey"],
                               max_age=300)
        def post(self, request): ...

Without a fresh grant the request is rejected with 403 and a structured
challenge envelope (challenge_id, factors, scope) — no redirects, the same
contract for SPA and mobile. The listed factors are interchangeable: any
one of them completes the challenge. Factor implementations are registered
by stapel-auth (or a host project) via ``register_factor``; the mechanism —
challenge store, grant store, decorator, OpenAPI annotation — lives here.

The same package owns :class:`OneTimeCodeStore` (``codes.py``): the TTL-scoped,
hashed store an OTP flow keeps its codes in instead of a database table. It is
storage only — lifetimes, attempt budgets and delivery stay with the caller.

Client cycle: 403 with ``verification`` → run the factor UI against the
auth service's verification endpoints → retry the original request (the
grant is stored server-side per user+scope; stateless clients may instead
send the X-Verification-Token returned on completion).

See docs: flows-and-verification.md in the stapel workspace.
"""

from .codes import (
    CodeCheck,
    CodeOutcome,
    IssuedCode,
    OneTimeCodeStore,
    StoreUnavailable,
)
from .decorators import VERIFICATION_LEVELS, requires_verification
from .factors import (
    FACTOR_STRENGTHS,
    VerificationFactor,
    factor_registry,
    register_factor,
    strong_factors,
)
from .grants import (
    complete_challenge,
    create_challenge,
    get_challenge,
    grant_verification,
    has_grant,
    verification_enrollment_payload,
    verification_error_payload,
)
from .policy import (
    get_user_policy,
    invalidate_policy_cache,
)

__all__ = [
    "requires_verification",
    "CodeCheck",
    "CodeOutcome",
    "IssuedCode",
    "OneTimeCodeStore",
    "StoreUnavailable",
    "VERIFICATION_LEVELS",
    "FACTOR_STRENGTHS",
    "VerificationFactor",
    "factor_registry",
    "register_factor",
    "strong_factors",
    "create_challenge",
    "get_challenge",
    "complete_challenge",
    "grant_verification",
    "has_grant",
    "verification_enrollment_payload",
    "verification_error_payload",
    "get_user_policy",
    "invalidate_policy_cache",
]
