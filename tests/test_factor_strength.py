"""VerificationFactor.strength: default, override, registry surfaces."""
import pytest

from stapel_core.verification import (
    FACTOR_STRENGTHS,
    VerificationFactor,
    factor_registry,
    register_factor,
    strong_factors,
)


class WeakByDefaultFactor(VerificationFactor):
    id = "email_code"

    def verify(self, user, challenge, payload):  # pragma: no cover
        return False


class TotpLikeFactor(VerificationFactor):
    id = "totp_like"
    strength = "strong"

    def verify(self, user, challenge, payload):  # pragma: no cover
        return False


class UnavailableStrongFactor(VerificationFactor):
    id = "passkey_like"
    strength = "strong"

    def available_for(self, user):
        return False

    def verify(self, user, challenge, payload):  # pragma: no cover
        return False


@pytest.fixture(autouse=True)
def _isolated_registry():
    factor_registry.clear()
    yield
    factor_registry.clear()


def test_strength_defaults_to_weak():
    # Canon: an email code alone is not 2FA — a factor is weak unless the
    # registrar explicitly marks it strong.
    assert WeakByDefaultFactor().strength == "weak"


def test_strength_override_to_strong():
    assert TotpLikeFactor().strength == "strong"


def test_register_rejects_invalid_strength():
    class BogusFactor(VerificationFactor):
        id = "bogus"
        strength = "super"

        def verify(self, user, challenge, payload):  # pragma: no cover
            return False

    with pytest.raises(ValueError, match="strength"):
        register_factor(BogusFactor())
    assert "bogus" not in factor_registry.names()


def test_describe_exposes_strength():
    register_factor(WeakByDefaultFactor())
    register_factor(TotpLikeFactor())
    assert factor_registry.describe() == [
        {"id": "email_code", "strength": "weak"},
        {"id": "totp_like", "strength": "strong"},
    ]


def test_strong_names_filters_weak():
    register_factor(WeakByDefaultFactor())
    register_factor(TotpLikeFactor())
    register_factor(UnavailableStrongFactor())
    assert factor_registry.strong_names() == ["passkey_like", "totp_like"]


def test_strong_factors_respects_availability():
    register_factor(WeakByDefaultFactor())
    register_factor(TotpLikeFactor())
    register_factor(UnavailableStrongFactor())
    # passkey_like is strong but not available for this user; email_code is
    # available but weak -> only totp_like counts as usable 2FA.
    assert strong_factors(user=object()) == ["totp_like"]


def test_strengths_constant():
    assert FACTOR_STRENGTHS == ("strong", "weak")
