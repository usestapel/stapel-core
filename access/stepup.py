"""Step-up on HIGH admin operations (admin-suite AS-6, §3.8, Q8a).

A HIGH-required admin mutation — ``delete`` in the standard preset, or any
operation a model declares at a step-up level — additionally requires a
*fresh* verification grant: the mandate (AS-1) decides whether a role *may*
perform the operation, step-up decides whether it was re-proven recently.
The policy is read from ``STAPEL_ACCESS["STEP_UP"]`` and enforced in
:class:`stapel_core.django.admin.base.StapelModelAdmin`.

**Convergence — no hook needed in stapel-auth.** The grant checked here is a
``stapel_core.verification`` grant, i.e. the *same* store stapel-auth's
step-up flow (``@requires_verification``) and the legacy ``/totp/step-up/``
bridge write to (scope ``sensitive``, max_age ``900`` — the defaults here
match on purpose).

**The channels the gate reads, and why there are three (0.45.0).** Until
0.44.1 there was effectively one: a grant in *this process's*
``django.core.cache.cache``, which Django namespaces under the service's own
``KEY_PREFIX``. The claim above ("completing step-up anywhere satisfies the
admin gate") therefore held only inside one prefix, and
:func:`step_up_denied_message` pointed operators at a cross-service flow that
could not satisfy the check. The ``X-Verification-Token`` fallback was
unreachable — this module never passed a token, and a browser form POST
cannot set a header anyway. So the gate's only satisfiable path was invisible
to the client it guards. What it reads now:

1. **The fleet-wide grant store.** ``verification.grants`` moved onto the
   shared namespace (:mod:`stapel_core.core.fleet_cache`), keyed by the
   fleet-stable ``user.pk``. Step-up completed in the auth service is now
   genuinely visible here — the property this module always claimed.
2. **The session.** ``request.session`` is the one credential the admin
   browser *does* carry on every subsequent form POST. A proof pinned there
   (:func:`record_step_up_in_session`) survives the confirm-form round trip
   and keeps working where the cache is not shared (LocMem, split Redis DBs).
3. **A presented verification token**, read from ``X-Verification-Token``
   (API clients) *or* the ``verification_token`` query parameter / form field
   (a browser returning from an auth-service step-up redirect). A token
   accepted at a view is pinned onto the session (:func:`adopt_step_up_token`)
   so the rest of the flow does not have to carry it.

**Degradation (admin-suite §3.7).** When no verification factor is registered
(no stapel-auth installed, no host factor) a grant can never be obtained, so
enforcing step-up would brick every HIGH operation permanently. Step-up
therefore self-disables until a factor exists — behavior falls back to the
AS-1/AS-3 mandate alone (the prior opt-in cascade). Q8a's ``ENFORCE=True``
default only takes effect once the mechanism is present.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from .exceptions import AccessConfigError
from .levels import Level

#: Keys accepted in ``STAPEL_ACCESS["STEP_UP"]``.
STEP_UP_KEYS = frozenset({"ENFORCE", "LEVELS", "SCOPE", "MAX_AGE"})

#: ``request.session`` key holding ``{scope: granted_at}``. The session cookie
#: is what an admin browser actually carries, so this is the channel a form
#: POST can satisfy — a header is not.
SESSION_KEY = "stapel_step_up"

#: Query-parameter / form-field name carrying a verification token. Same value
#: the ``X-Verification-Token`` header carries; this is the spelling a browser
#: can produce (an auth-service step-up redirect appends it to the return URL).
TOKEN_PARAM = "verification_token"

#: Baseline the settings dict merges over (Q8a: enforced by default; the
#: standard preset's ``delete=HIGH`` is the canonical trigger; scope/max_age
#: match stapel-auth's step-up grant).
DEFAULT_STEP_UP: Mapping[str, Any] = {
    "ENFORCE": True,
    "LEVELS": ("high",),
    "SCOPE": "sensitive",
    "MAX_AGE": 900,
}


def _parse_step_up(raw: Any) -> dict[str, Any]:
    """Validate and normalize ``STAPEL_ACCESS["STEP_UP"]`` over the defaults.

    ``LEVELS`` is normalized to a ``frozenset[str]`` of lowercase level names
    (``{"high"}``) so a required level matches by ``required.name.lower()``.
    A level may only be one of low/mid/high — step-up on the SUPERUSER /
    FORBIDDEN sentinels is meaningless (those operations are already barred).
    """
    source = "STAPEL_ACCESS['STEP_UP']"
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise AccessConfigError(
            f"{source} must be a dict or None, got {type(raw).__name__}"
        )
    unknown = set(raw) - STEP_UP_KEYS
    if unknown:
        raise AccessConfigError(f"{source} has unknown keys: {sorted(unknown)}")

    cfg: dict[str, Any] = dict(DEFAULT_STEP_UP)
    cfg["LEVELS"] = frozenset(name.lower() for name in DEFAULT_STEP_UP["LEVELS"])

    if "ENFORCE" in raw:
        cfg["ENFORCE"] = bool(raw["ENFORCE"])
    if "SCOPE" in raw:
        scope = raw["SCOPE"]
        if not isinstance(scope, str) or not scope.strip():
            raise AccessConfigError(f"{source}['SCOPE'] must be a non-empty string")
        cfg["SCOPE"] = scope
    if "MAX_AGE" in raw:
        max_age = raw["MAX_AGE"]
        if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= 0:
            raise AccessConfigError(f"{source}['MAX_AGE'] must be a positive integer")
        cfg["MAX_AGE"] = max_age
    if "LEVELS" in raw:
        levels = raw["LEVELS"]
        if isinstance(levels, str) or not isinstance(levels, (list, tuple, frozenset, set)):
            raise AccessConfigError(
                f"{source}['LEVELS'] must be a list of level names (e.g. ['high'])"
            )
        cfg["LEVELS"] = frozenset(
            Level.parse(level, clearance_only=True).name.lower() for level in levels
        )
    return cfg


def step_up_config() -> dict[str, Any]:
    """The parsed, validated step-up policy (defaults merged in)."""
    from .conf import access_settings

    return _parse_step_up(access_settings.STEP_UP)


def step_up_enforced() -> bool:
    """The raw ENFORCE flag (Q8a default True) — ignores capability."""
    return bool(step_up_config()["ENFORCE"])


def step_up_capable() -> bool:
    """Whether a verification grant is obtainable at all (degradation gate).

    True once any verification factor is registered — stapel-auth registers
    otp/totp/passkey in its ``ready()``; a host may register its own. With
    nothing registered a grant can never be minted, so step-up self-disables.
    """
    from stapel_core.verification.factors import factor_registry

    return bool(factor_registry.names())


def step_up_active() -> bool:
    """Enforcement is on *and* the grant mechanism is present."""
    return step_up_enforced() and step_up_capable()


def action_requires_step_up(model: type, action: str) -> bool:
    """Whether *action* on *model* is a step-up-gated (HIGH-class) operation."""
    from .declaration import effective_access

    required = effective_access(model).required(action)
    return required.name.lower() in step_up_config()["LEVELS"]


def step_up_token(request) -> str | None:
    """The verification token *request* presents, on any channel a client has.

    Header first (API clients), then the form field, then the query string —
    the two spellings a browser can produce. Returns ``None`` when the request
    carries none, which is the ordinary case for an admin page load.
    """
    if request is None:
        return None
    from stapel_core.verification.decorators import TOKEN_HEADER

    for container, key in (
        (getattr(request, "headers", None), TOKEN_HEADER),
        (getattr(request, "POST", None), TOKEN_PARAM),
        (getattr(request, "GET", None), TOKEN_PARAM),
    ):
        if container is None:
            continue
        value = container.get(key)
        if value:
            return str(value)
    return None


def _session_step_ups(request) -> dict:
    session = getattr(request, "session", None)
    if session is None:
        return {}
    stored = session.get(SESSION_KEY)
    return stored if isinstance(stored, dict) else {}


def session_step_up_fresh(request, *, scope: str | None = None) -> bool:
    """Whether the session on *request* records a step-up still inside MAX_AGE.

    Freshness is judged against the *current* policy, not the one in force
    when the proof was written, so shortening ``MAX_AGE`` tightens live
    sessions immediately. A timestamp in the future is refused rather than
    trusted — the server wrote it, so a forward one means tampering or a
    clock that cannot be reasoned about.
    """
    cfg = step_up_config()
    granted_at = _session_step_ups(request).get(scope or cfg["SCOPE"])
    if not isinstance(granted_at, (int, float)) or isinstance(granted_at, bool):
        return False
    age = time.time() - granted_at
    return 0 <= age <= cfg["MAX_AGE"]


def record_step_up_in_session(request, *, scope: str | None = None) -> bool:
    """Pin a completed step-up onto the session the browser already carries.

    The host's step-up view calls this after a factor succeeds, so the admin
    gate is satisfiable from a session-cookie flow with no header and no
    shared cache. Returns False when the request has no session (an API-only
    deployment), which is not an error — the grant store is still the primary
    channel.
    """
    session = getattr(request, "session", None)
    if session is None:
        return False
    scope = scope or step_up_config()["SCOPE"]
    recorded = dict(_session_step_ups(request))
    recorded[scope] = int(time.time())
    session[SESSION_KEY] = recorded
    if hasattr(session, "modified"):
        session.modified = True
    return True


def adopt_step_up_token(request, user) -> bool:
    """Validate a token presented on *request* and pin it onto the session.

    Called from the admin *views* (never from ``has_*_permission``, which must
    stay side-effect free). Without this, a browser arriving from an
    auth-service redirect at ``?verification_token=...`` would pass the page
    load and then be refused by the confirm-form POST, which no longer carries
    the query string.
    """
    if user is None:
        return False
    token = step_up_token(request)
    if not token:
        return False
    from stapel_core.verification.grants import has_grant

    scope = step_up_config()["SCOPE"]
    if not has_grant(user, scope, token=token):
        return False
    return record_step_up_in_session(request, scope=scope)


def has_fresh_step_up(user, request=None) -> bool:
    """Whether *user* holds a fresh step-up proof on any channel.

    *request* is optional only so callers with no request (the
    ``access_report`` uptake aggregate) keep working; every enforcement path
    passes it, because without it the gate can read nothing the client
    presents.
    """
    from stapel_core.verification.grants import has_grant

    scope = step_up_config()["SCOPE"]
    if has_grant(user, scope, token=step_up_token(request)):
        return True
    return session_step_up_fresh(request, scope=scope)


def step_up_blocks(user, model: type, action: str, *, request=None) -> bool:
    """True when step-up is active, *action* is gated, and no fresh proof exists.

    The single predicate StapelModelAdmin consults. Cheap and side-effect
    free — a cache read plus a session read at worst; safe to call from
    ``has_*_permission``.
    """
    if user is None or not step_up_active():
        return False
    if not action_requires_step_up(model, action):
        return False
    return not has_fresh_step_up(user, request)


def step_up_denied_message(model: type, action: str) -> str:
    """Educational 403 body — how to obtain the grant (no web flow in core).

    Every path named here is one the reader can actually take: the grant
    store is fleet-wide, so a factor completed in the auth service counts
    here, and the token spellings include the two a browser can produce.
    """
    from stapel_core.verification.decorators import TOKEN_HEADER

    cfg = step_up_config()
    return (
        f"Step-up verification required for the '{action}' operation on "
        f"{model._meta.label}. This is a HIGH-clearance action; obtain a "
        f"fresh verification grant for scope '{cfg['SCOPE']}' (valid "
        f"{cfg['MAX_AGE']}s) through your auth service's step-up flow "
        f"(complete an OTP/TOTP/passkey factor), then retry. The grant is "
        "written to the fleet-wide verification namespace this service reads, "
        "so completing the factor in another service of the same fleet counts "
        f"here. A client holding the returned verification token may present "
        f"it instead — as the '{TOKEN_PARAM}' query parameter or form field, "
        f"or the '{TOKEN_HEADER}' header; an accepted token is pinned onto "
        "your session for the rest of the flow."
    )


def record_step_up_denied(user, model: type, action: str) -> None:
    """Fire the :data:`~stapel_core.access.signals.step_up_denied` signal."""
    from .signals import step_up_denied

    step_up_denied.send(
        sender=model,
        user=user,
        label=model._meta.label,
        action=action,
        scope=step_up_config()["SCOPE"],
    )


__all__ = [
    "DEFAULT_STEP_UP",
    "SESSION_KEY",
    "STEP_UP_KEYS",
    "TOKEN_PARAM",
    "action_requires_step_up",
    "adopt_step_up_token",
    "has_fresh_step_up",
    "record_step_up_denied",
    "record_step_up_in_session",
    "session_step_up_fresh",
    "step_up_token",
    "step_up_active",
    "step_up_blocks",
    "step_up_capable",
    "step_up_config",
    "step_up_denied_message",
    "step_up_enforced",
]
