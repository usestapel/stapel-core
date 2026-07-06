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
match on purpose). Completing step-up anywhere in the session satisfies the
admin gate; this module only *reads*.

**Degradation (admin-suite §3.7).** When no verification factor is registered
(no stapel-auth installed, no host factor) a grant can never be obtained, so
enforcing step-up would brick every HIGH operation permanently. Step-up
therefore self-disables until a factor exists — behavior falls back to the
AS-1/AS-3 mandate alone (the prior opt-in cascade). Q8a's ``ENFORCE=True``
default only takes effect once the mechanism is present.
"""
from __future__ import annotations

from typing import Any, Mapping

from .exceptions import AccessConfigError
from .levels import Level

#: Keys accepted in ``STAPEL_ACCESS["STEP_UP"]``.
STEP_UP_KEYS = frozenset({"ENFORCE", "LEVELS", "SCOPE", "MAX_AGE"})

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


def has_fresh_step_up(user) -> bool:
    """Whether *user* holds a fresh verification grant for the step-up scope."""
    from stapel_core.verification.grants import has_grant

    return has_grant(user, step_up_config()["SCOPE"])


def step_up_blocks(user, model: type, action: str) -> bool:
    """True when step-up is active, *action* is gated, and no fresh grant exists.

    The single predicate StapelModelAdmin consults. Cheap and side-effect
    free — a cache read at worst; safe to call from ``has_*_permission``.
    """
    if user is None or not step_up_active():
        return False
    if not action_requires_step_up(model, action):
        return False
    return not has_fresh_step_up(user)


def step_up_denied_message(model: type, action: str) -> str:
    """Educational 403 body — how to obtain the grant (no web flow in core)."""
    cfg = step_up_config()
    return (
        f"Step-up verification required for the '{action}' operation on "
        f"{model._meta.label}. This is a HIGH-clearance action; obtain a "
        f"fresh verification grant for scope '{cfg['SCOPE']}' (valid "
        f"{cfg['MAX_AGE']}s) through your auth service's step-up flow "
        f"(complete an OTP/TOTP/passkey factor), then retry."
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
    "STEP_UP_KEYS",
    "action_requires_step_up",
    "has_fresh_step_up",
    "record_step_up_denied",
    "step_up_active",
    "step_up_blocks",
    "step_up_capable",
    "step_up_config",
    "step_up_denied_message",
    "step_up_enforced",
]
