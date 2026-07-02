"""Verification factor registry.

A factor knows how to initiate (send a code / produce WebAuthn options)
and how to verify a user's response. stapel-auth registers otp_email,
otp_phone, totp and passkey; host projects add their own by dotted path
or register_factor() — the same escape-hatch pattern as payment providers
and notification channels.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod


class VerificationFactor(ABC):
    """One way for a user to prove presence (OTP, TOTP, passkey, ...)."""

    #: machine name, e.g. "otp_email"
    id: str = ""

    def available_for(self, user) -> bool:
        """Whether this user can use the factor (has email/TOTP/passkey)."""
        return True

    def initiate(self, user, challenge: dict) -> dict:
        """Kick off the factor (send the code, build WebAuthn options).

        Returns client-facing data merged into the initiate response
        (e.g. masked destination, webauthn options). Default: nothing.
        """
        return {}

    @abstractmethod
    def verify(self, user, challenge: dict, payload: dict) -> bool:
        """Check the user's proof (code, assertion). True = passed."""


class FactorRegistry:
    def __init__(self) -> None:
        self._factors: dict[str, VerificationFactor] = {}
        self._lock = threading.Lock()

    def register(self, factor: VerificationFactor) -> None:
        if not factor.id:
            raise ValueError("factor must define a non-empty id")
        with self._lock:
            self._factors[factor.id] = factor

    def get(self, factor_id: str) -> VerificationFactor:
        try:
            return self._factors[factor_id]
        except KeyError:
            raise KeyError(
                f"verification factor {factor_id!r} is not registered "
                "(is stapel-auth installed / factor registered in ready()?)"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._factors)

    def available_for(self, user, factor_ids: list[str]) -> list[str]:
        """Subset of *factor_ids* the user can actually complete."""
        out = []
        for fid in factor_ids:
            factor = self._factors.get(fid)
            if factor is not None and factor.available_for(user):
                out.append(fid)
        return out

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            self._factors.clear()


factor_registry = FactorRegistry()


def register_factor(factor: VerificationFactor | str) -> None:
    """Register a factor instance or a dotted path to a factor class."""
    if isinstance(factor, str):
        from django.utils.module_loading import import_string

        factor = import_string(factor)()
    factor_registry.register(factor)


def load_configured_factors() -> None:
    """Register factors listed in STAPEL_VERIFICATION['EXTRA_FACTORS']."""
    from .conf import verification_settings

    for dotted in verification_settings.EXTRA_FACTORS or []:
        register_factor(dotted)


__all__: list[str] = [
    "VerificationFactor",
    "FactorRegistry",
    "factor_registry",
    "register_factor",
    "load_configured_factors",
]
