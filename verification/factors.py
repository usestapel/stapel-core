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


#: Valid values for :attr:`VerificationFactor.strength`.
FACTOR_STRENGTHS = ("strong", "weak")


class VerificationFactor(ABC):
    """One way for a user to prove presence (OTP, TOTP, passkey, ...)."""

    #: machine name, e.g. "otp_email"
    id: str = ""

    #: "strong" | "weak" — whether completing this factor counts as a real
    #: second factor. Canon: an email code alone is NOT 2FA (it proves reach
    #: to the same channel that resets the password), so the default is
    #: "weak"; registrars mark totp/passkey/otp_phone as "strong". Strict
    #: "user has 2FA" checks (org require_mfa policies, mfa_status APIs)
    #: must count strong factors only.
    strength: str = "weak"

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
        #: ids claimed by the host project through EXTRA_FACTORS — a library
        #: registration for the same id loses, whatever the app order is.
        self._pinned: set[str] = set()
        self._lock = threading.Lock()

    def register(self, factor: VerificationFactor, *, pin: bool = False) -> None:
        """Put *factor* in the registry under its ``id``.

        ``pin=True`` marks the id as **host-owned**: later library
        registrations of the same id are ignored, so the host's override
        does not depend on where its app sits in ``INSTALLED_APPS``.
        Only :func:`load_configured_factors` (i.e. an explicit
        ``STAPEL_VERIFICATION['EXTRA_FACTORS']`` declaration) pins; a pinned
        id can still be re-pinned (last host declaration wins).
        """
        if not factor.id:
            raise ValueError("factor must define a non-empty id")
        if factor.strength not in FACTOR_STRENGTHS:
            raise ValueError(
                f"factor {factor.id!r} has invalid strength "
                f"{factor.strength!r} (expected one of {FACTOR_STRENGTHS})"
            )
        with self._lock:
            if factor.id in self._pinned and not pin:
                return
            self._factors[factor.id] = factor
            if pin:
                self._pinned.add(factor.id)

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

    def describe(self) -> list[dict]:
        """Registry listing with attributes: ``[{"id", "strength"}, ...]``.

        The factor-status surface (auth's factor list / mfa_status) builds
        on this so clients can distinguish real 2FA factors from weak ones.
        """
        return [
            {"id": fid, "strength": self._factors[fid].strength}
            for fid in sorted(self._factors)
        ]

    def strong_names(self) -> list[str]:
        """Ids of registered factors with ``strength == "strong"``."""
        return sorted(
            fid for fid, f in self._factors.items() if f.strength == "strong"
        )

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
            self._pinned.clear()

    def pinned_names(self) -> list[str]:
        """Ids claimed by the host through ``EXTRA_FACTORS`` (introspection)."""
        return sorted(self._pinned)


factor_registry = FactorRegistry()


def register_factor(factor: VerificationFactor | str, *, pin: bool = False) -> None:
    """Register a factor instance or a dotted path to a factor class."""
    if isinstance(factor, str):
        from django.utils.module_loading import import_string

        factor = import_string(factor)()
    factor_registry.register(factor, pin=pin)


def strong_factors(user) -> list[str]:
    """Ids of STRONG registered factors this user can actually complete.

    The strict "does the user have 2FA" predicate: non-empty result means
    the user has at least one real second factor (totp/passkey/otp_phone —
    never a bare email code).
    """
    return factor_registry.available_for(user, factor_registry.strong_names())


def load_configured_factors() -> None:
    """Register factors listed in ``STAPEL_VERIFICATION['EXTRA_FACTORS']``.

    Called by ``stapel_core.django.apps.CommonDjangoConfig.ready()`` — the
    host only has to *declare* the dotted path, exactly as MODULE.md
    promises. (Before 0.16.1 the function had no caller anywhere in the
    framework, so a host that followed the documentation to the letter got
    a decorative setting and no warning; meettoday #124 had to call this
    loader from its own app layer to make a security fix real.)

    Order-independent: entries are registered *pinned*, so a factor id the
    host claims here beats any later library registration of the same id
    regardless of ``INSTALLED_APPS`` order — an app that already calls this
    loader itself (below the library, the pre-0.16.1 workaround) keeps
    working and simply re-pins the same class.

    A dotted path that cannot be imported or is not a valid factor raises
    ``ImproperlyConfigured`` at boot: a broken escape hatch is louder than
    a silent one.
    """
    from django.core.exceptions import ImproperlyConfigured

    from .conf import verification_settings

    for dotted in verification_settings.EXTRA_FACTORS or []:
        try:
            register_factor(dotted, pin=True)
        except Exception as exc:  # ImportError, ValueError, TypeError...
            raise ImproperlyConfigured(
                f"STAPEL_VERIFICATION['EXTRA_FACTORS'] entry {dotted!r} could "
                f"not be registered: {exc}"
            ) from exc


__all__: list[str] = [
    "FACTOR_STRENGTHS",
    "VerificationFactor",
    "FactorRegistry",
    "factor_registry",
    "register_factor",
    "strong_factors",
    "load_configured_factors",
]
