"""Challenge policy — tiered captcha strictness by network class.

The binary "captcha on/off" logic becomes a policy: given the request and
an endpoint action class, decide how hard the challenge should be.

Levels, ordered from most permissive to most restrictive::

    none < invisible < interactive < interactive+ratelimit < block

Captcha providers map a level onto their own mechanics (Turnstile:
managed/non-interactive vs forced interactive; reCAPTCHA: score threshold
vs explicit challenge). ``interactive+ratelimit`` additionally signals
rate-limit middleware via ``request.stapel_challenge_level`` — the captcha
layer itself does not rate-limit. ``block`` refuses the request outright
and is never produced by the default matrix: VPN users are legitimate,
blocking a network class is always an explicit host decision.

The policy is a replace-style dotted-path seam
(``STAPEL_CAPTCHA["CHALLENGE_POLICY"]``); :class:`MatrixChallengePolicy` is
the default implementation driven by ``stapel_core.netintel``.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

LEVEL_NONE = "none"
LEVEL_INVISIBLE = "invisible"
LEVEL_INTERACTIVE = "interactive"
LEVEL_INTERACTIVE_RATELIMIT = "interactive+ratelimit"
LEVEL_BLOCK = "block"

#: All levels, in strictness order — index = severity.
CHALLENGE_LEVELS = (
    LEVEL_NONE,
    LEVEL_INVISIBLE,
    LEVEL_INTERACTIVE,
    LEVEL_INTERACTIVE_RATELIMIT,
    LEVEL_BLOCK,
)

#: ip-kind → level defaults (docs/geo-network-trust.md §2). With the default
#: NullProvider every request is "unknown" → "invisible", which reproduces
#: the pre-policy behavior exactly (verify the token iff a backend is
#: configured).
DEFAULT_CHALLENGE_MATRIX = {
    "residential": LEVEL_INVISIBLE,
    "unknown": LEVEL_INVISIBLE,
    "datacenter": LEVEL_INTERACTIVE,
    "vpn": LEVEL_INTERACTIVE,
    "tor": LEVEL_INTERACTIVE_RATELIMIT,
}


def level_index(level: str) -> int:
    """Position of *level* in the strictness order (raises on unknown)."""
    try:
        return CHALLENGE_LEVELS.index(level)
    except ValueError:
        raise ValueError(
            f"unknown challenge level {level!r} "
            f"(expected one of {', '.join(CHALLENGE_LEVELS)})"
        ) from None


def level_gte(level: str, other: str) -> bool:
    """True when *level* is at least as strict as *other*."""
    return level_index(level) >= level_index(other)


def bump_level(level: str, steps: int = 1) -> str:
    """*level* raised by *steps*, saturating at ``block``."""
    index = max(0, min(level_index(level) + steps, len(CHALLENGE_LEVELS) - 1))
    return CHALLENGE_LEVELS[index]


class ChallengePolicy(ABC):
    """Decides how hard the captcha challenge is for a request+action."""

    @abstractmethod
    def level_for(self, request, action: str) -> str:
        """One of :data:`CHALLENGE_LEVELS` for this request and action."""


class MatrixChallengePolicy(ChallengePolicy):
    """Default policy: netintel ip-kind → matrix level → action override.

    1. ``stapel_core.netintel.classify_ip(client_ip(request)).kind``
       (fail-open: unknown when netintel is unconfigured/unavailable).
    2. ``STAPEL_CAPTCHA["CHALLENGE_MATRIX"]`` merged over
       :data:`DEFAULT_CHALLENGE_MATRIX`.
    3. ``STAPEL_CAPTCHA["ACTION_OVERRIDES"][action]`` applied:
       ``"+1"`` bumps one level; a ``{kind: level}`` dict replaces the level
       for matching kinds (a per-kind ``"+1"`` also bumps).
    """

    def level_for(self, request, action: str = "default") -> str:
        from stapel_core.netintel import classify_ip, client_ip

        from .conf import captcha_settings

        kind = classify_ip(client_ip(request)).kind
        matrix = {**DEFAULT_CHALLENGE_MATRIX, **(captcha_settings.CHALLENGE_MATRIX or {})}
        level = matrix.get(kind, matrix.get("unknown", LEVEL_INVISIBLE))

        override = (captcha_settings.ACTION_OVERRIDES or {}).get(action)
        if override == "+1":
            level = bump_level(level)
        elif isinstance(override, dict) and kind in override:
            value = override[kind]
            level = bump_level(level) if value == "+1" else value

        if level not in CHALLENGE_LEVELS:
            logger.warning(
                "challenge policy produced unknown level %r for action=%s "
                "kind=%s — falling back to %r",
                level, action, kind, LEVEL_INVISIBLE,
            )
            level = LEVEL_INVISIBLE
        return level


def get_challenge_policy() -> ChallengePolicy:
    """The configured policy (``STAPEL_CAPTCHA["CHALLENGE_POLICY"]``).

    Accepts a dotted path, a class or an instance. Fails open to
    :class:`MatrixChallengePolicy` on misconfiguration — the captcha layer
    must never take an endpoint down.
    """
    from .conf import captcha_settings

    value = captcha_settings.CHALLENGE_POLICY
    try:
        if isinstance(value, str):
            from django.utils.module_loading import import_string

            value = import_string(value)
        if isinstance(value, type):
            value = value()
        if not isinstance(value, ChallengePolicy):
            raise TypeError(f"{value!r} is not a ChallengePolicy")
        return value
    except Exception as exc:
        logger.warning(
            "STAPEL_CAPTCHA['CHALLENGE_POLICY'] (%r) unusable (%s: %s) — "
            "falling back to MatrixChallengePolicy",
            captcha_settings.CHALLENGE_POLICY, type(exc).__name__, exc,
        )
        return MatrixChallengePolicy()


__all__ = [
    "CHALLENGE_LEVELS",
    "ChallengePolicy",
    "DEFAULT_CHALLENGE_MATRIX",
    "LEVEL_BLOCK",
    "LEVEL_INTERACTIVE",
    "LEVEL_INTERACTIVE_RATELIMIT",
    "LEVEL_INVISIBLE",
    "LEVEL_NONE",
    "MatrixChallengePolicy",
    "bump_level",
    "get_challenge_policy",
    "level_gte",
    "level_index",
]
