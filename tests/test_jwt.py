import time
import pytest
from datetime import timedelta

from stapel_core.core.config import JWTConfig
from stapel_core.core.jwt_handler import JWTHandler
from stapel_core.core.token_blacklist import TokenBlacklist
from stapel_core.core.token_manager import TokenManager


# ---------------------------------------------------------------------------
# Shared configs
# ---------------------------------------------------------------------------

def _hs256(**kwargs) -> JWTConfig:
    defaults = dict(
        secret_key="test-secret-key-for-testing-purposes-only-32b",
        algorithm="HS256",
        issuer="test-iss",
        audience=None,
        access_token_lifetime=timedelta(minutes=15),
        refresh_token_lifetime=timedelta(days=1),
    )
    defaults.update(kwargs)
    return JWTConfig(**defaults)


HS256 = _hs256()

USER = {"user_id": "u1", "email": "u@example.com"}


# ---------------------------------------------------------------------------
# JWTConfig
# ---------------------------------------------------------------------------

class TestJWTConfig:
    def test_hs256_requires_secret(self):
        with pytest.raises(ValueError, match="secret_key"):
            JWTConfig(
                secret_key="",
                algorithm="HS256",
                audience=None,
                access_token_lifetime=timedelta(minutes=15),
                refresh_token_lifetime=timedelta(days=1),
            )

    def test_unsupported_algorithm_raises(self):
        with pytest.raises(ValueError):
            JWTConfig(
                secret_key="s",
                algorithm="RS512",
                audience=None,
                access_token_lifetime=timedelta(minutes=15),
                refresh_token_lifetime=timedelta(days=1),
            )

    def test_access_longer_than_refresh_raises(self):
        with pytest.raises(ValueError):
            _hs256(
                access_token_lifetime=timedelta(days=7),
                refresh_token_lifetime=timedelta(minutes=15),
            )

    def test_invalid_samesite_raises(self):
        with pytest.raises(ValueError):
            _hs256(cookie_samesite="Always")

    def test_can_sign_and_verify_hs256(self):
        assert HS256.can_sign()
        assert HS256.can_verify()

    def test_get_signing_key_hs256(self):
        assert HS256.get_signing_key() == "test-secret-key-for-testing-purposes-only-32b"

    def test_get_verification_key_hs256(self):
        assert HS256.get_verification_key() == "test-secret-key-for-testing-purposes-only-32b"


# ---------------------------------------------------------------------------
# JWTHandler — HS256
# ---------------------------------------------------------------------------

class TestJWTHandlerHS256:
    def setup_method(self):
        self.h = JWTHandler(HS256)

    def test_generate_token_pair_returns_two_different_tokens(self):
        access, refresh = self.h.generate_token_pair(USER)
        assert access and refresh
        assert access != refresh

    def test_decode_access_token_payload(self):
        access, _ = self.h.generate_token_pair(USER)
        payload = self.h.decode_token(access)
        assert payload is not None
        assert payload["user_id"] == "u1"
        assert payload["token_type"] == "access"

    def test_decode_refresh_token_payload(self):
        _, refresh = self.h.generate_token_pair(USER)
        payload = self.h.decode_token(refresh)
        assert payload is not None
        assert payload["token_type"] == "refresh"

    def test_decode_invalid_signature_returns_none(self):
        access, _ = self.h.generate_token_pair(USER)
        bad = JWTHandler(_hs256(secret_key="wrong-secret-key-for-testing-only-32bytes"))
        assert bad.decode_token(access) is None

    def test_decode_expired_token_returns_none(self):
        short = _hs256(
            access_token_lifetime=timedelta(seconds=1),
            refresh_token_lifetime=timedelta(seconds=2),
        )
        h = JWTHandler(short)
        access, _ = h.generate_token_pair(USER)
        time.sleep(1.1)
        assert h.decode_token(access) is None

    def test_decode_unverified_expired_token_succeeds(self):
        short = _hs256(
            access_token_lifetime=timedelta(seconds=1),
            refresh_token_lifetime=timedelta(seconds=2),
        )
        h = JWTHandler(short)
        access, _ = h.generate_token_pair(USER)
        time.sleep(1.1)
        payload = h.decode_token(access, verify=False)
        assert payload is not None

    def test_is_token_expired_false_for_valid(self):
        access, _ = self.h.generate_token_pair(USER)
        assert not self.h.is_token_expired(access)

    def test_is_token_expired_true_for_expired(self):
        short = _hs256(
            access_token_lifetime=timedelta(seconds=1),
            refresh_token_lifetime=timedelta(seconds=2),
        )
        h = JWTHandler(short)
        access, _ = h.generate_token_pair(USER)
        time.sleep(1.1)
        assert h.is_token_expired(access)

    def test_validate_token_type_access(self):
        access, refresh = self.h.generate_token_pair(USER)
        assert self.h.validate_token_type(access, "access")
        assert not self.h.validate_token_type(access, "refresh")

    def test_validate_token_type_refresh(self):
        _, refresh = self.h.generate_token_pair(USER)
        assert self.h.validate_token_type(refresh, "refresh")
        assert not self.h.validate_token_type(refresh, "access")

    def test_extract_user_data_strips_jwt_claims(self):
        access, _ = self.h.generate_token_pair(USER)
        payload = self.h.decode_token(access)
        user_data = self.h.extract_user_data(payload)
        assert "user_id" in user_data
        assert "email" in user_data
        assert "exp" not in user_data
        assert "iat" not in user_data
        assert "jti" not in user_data
        assert "token_type" not in user_data

    def test_get_token_expiration_returns_datetime(self):
        access, _ = self.h.generate_token_pair(USER)
        exp = self.h.get_token_expiration(access)
        assert exp is not None

    def test_is_token_near_expiry_false_for_fresh(self):
        access, _ = self.h.generate_token_pair(USER)
        assert not self.h.is_token_near_expiry(access)

    def test_generate_access_token_only(self):
        token = self.h.generate_access_token(USER)
        assert token
        assert self.h.validate_token_type(token, "access")

    def test_generate_refresh_token_only(self):
        token = self.h.generate_refresh_token(USER)
        assert token
        assert self.h.validate_token_type(token, "refresh")

    def test_missing_user_identifier_raises(self):
        with pytest.raises(ValueError):
            self.h.generate_token_pair({"name": "no_user_id"})

    def test_jti_is_unique(self):
        access1, _ = self.h.generate_token_pair(USER)
        access2, _ = self.h.generate_token_pair(USER)
        p1 = self.h.decode_token(access1)
        p2 = self.h.decode_token(access2)
        assert p1["jti"] != p2["jti"]

    def test_issuer_in_payload(self):
        access, _ = self.h.generate_token_pair(USER)
        payload = self.h.decode_token(access)
        assert payload["iss"] == "test-iss"

    def test_jwks_not_available_for_hs256(self):
        assert self.h.get_jwks() is None


# ---------------------------------------------------------------------------
# JWTHandler — RS256 (requires cryptography)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rsa_keys():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    priv = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    private_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


class TestJWTHandlerRS256:
    def _make_handler(self, private_pem, public_pem):
        cfg = JWTConfig(
            algorithm="RS256",
            private_key=private_pem,
            public_key=public_pem,
            issuer="rs256-test",
            audience=None,
            access_token_lifetime=timedelta(minutes=15),
            refresh_token_lifetime=timedelta(days=1),
        )
        return JWTHandler(cfg)

    def test_generate_and_decode(self, rsa_keys):
        priv, pub = rsa_keys
        h = self._make_handler(priv, pub)
        access, _ = h.generate_token_pair(USER)
        payload = h.decode_token(access)
        assert payload["user_id"] == "u1"

    def test_verify_only_config(self, rsa_keys):
        priv, pub = rsa_keys
        signer = self._make_handler(priv, pub)
        verify_cfg = JWTConfig(
            algorithm="RS256",
            public_key=pub,
            issuer="rs256-test",
            audience=None,
            access_token_lifetime=timedelta(minutes=15),
            refresh_token_lifetime=timedelta(days=1),
        )
        verifier = JWTHandler(verify_cfg)
        access, _ = signer.generate_token_pair(USER)
        payload = verifier.decode_token(access)
        assert payload["user_id"] == "u1"

    def test_cannot_sign_without_private_key(self, rsa_keys):
        _, pub = rsa_keys
        cfg = JWTConfig(
            algorithm="RS256",
            public_key=pub,
            audience=None,
            access_token_lifetime=timedelta(minutes=15),
            refresh_token_lifetime=timedelta(days=1),
        )
        h = JWTHandler(cfg)
        assert not cfg.can_sign()
        with pytest.raises(ValueError):
            h.generate_token_pair(USER)

    def test_jwks_returns_rsa_key(self, rsa_keys):
        priv, pub = rsa_keys
        h = self._make_handler(priv, pub)
        jwks = h.get_jwks()
        assert jwks is not None
        assert "keys" in jwks
        assert jwks["keys"][0]["kty"] == "RSA"


# ---------------------------------------------------------------------------
# TokenBlacklist
# ---------------------------------------------------------------------------

class TestTokenBlacklist:
    def setup_method(self):
        self.bl = TokenBlacklist(key_prefix="test_bl")

    def test_not_blacklisted_by_default(self):
        assert not self.bl.is_blacklisted("jti-unknown")

    def test_blacklist_and_check(self):
        result = self.bl.blacklist_token("jti-1", timedelta(seconds=300))
        assert result is True
        assert self.bl.is_blacklisted("jti-1")

    def test_different_jti_not_affected(self):
        self.bl.blacklist_token("jti-a", timedelta(seconds=300))
        assert not self.bl.is_blacklisted("jti-b")

    def test_remove_from_blacklist(self):
        self.bl.blacklist_token("jti-2", timedelta(seconds=300))
        self.bl.remove_from_blacklist("jti-2")
        assert not self.bl.is_blacklisted("jti-2")

    def test_clear_all(self):
        self.bl.blacklist_token("jti-3", timedelta(seconds=300))
        self.bl.blacklist_token("jti-4", timedelta(seconds=300))
        self.bl.clear_all()
        assert not self.bl.is_blacklisted("jti-3")
        assert not self.bl.is_blacklisted("jti-4")

    def test_key_prefix_isolation(self):
        bl2 = TokenBlacklist(key_prefix="other_prefix")
        self.bl.blacklist_token("shared-jti", timedelta(seconds=300))
        assert not bl2.is_blacklisted("shared-jti")


# ---------------------------------------------------------------------------
# TokenManager
# ---------------------------------------------------------------------------

class TestTokenManager:
    def setup_method(self):
        self.bl = TokenBlacklist(key_prefix="tm_test")
        self.manager = TokenManager(HS256, blacklist=self.bl)

    def test_create_tokens(self):
        access, refresh = self.manager.create_tokens(USER)
        assert access and refresh

    def test_create_access_token_only(self):
        token = self.manager.create_access_token(USER)
        assert token

    def test_validate_access_token_returns_user_data(self):
        access, _ = self.manager.create_tokens(USER)
        data = self.manager.validate_access_token(access)
        assert data is not None
        assert data["user_id"] == "u1"

    def test_validate_access_token_rejects_refresh_token(self):
        _, refresh = self.manager.create_tokens(USER)
        assert self.manager.validate_access_token(refresh) is None

    def test_validate_refresh_token_returns_user_data(self):
        _, refresh = self.manager.create_tokens(USER)
        data = self.manager.validate_refresh_token(refresh)
        assert data is not None
        assert data["user_id"] == "u1"

    def test_validate_refresh_token_rejects_access_token(self):
        access, _ = self.manager.create_tokens(USER)
        assert self.manager.validate_refresh_token(access) is None

    def test_refresh_access_token(self):
        _, refresh = self.manager.create_tokens(USER)
        new_access = self.manager.refresh_access_token(refresh)
        assert new_access is not None
        data = self.manager.validate_access_token(new_access)
        assert data["user_id"] == "u1"

    def test_refresh_with_load_user_data(self):
        _, refresh = self.manager.create_tokens(USER)
        fresh = {"user_id": "u1", "role": "admin"}
        new_access = self.manager.refresh_access_token(
            refresh,
            load_user_data=lambda uid: fresh if uid == "u1" else None,
        )
        assert new_access is not None
        payload = self.manager.validate_access_token(new_access)
        assert payload.get("role") == "admin"

    def test_refresh_with_unknown_user_returns_none(self):
        _, refresh = self.manager.create_tokens(USER)
        new_access = self.manager.refresh_access_token(
            refresh,
            load_user_data=lambda uid: None,
        )
        assert new_access is None

    def test_get_token_jti(self):
        access, _ = self.manager.create_tokens(USER)
        jti = self.manager.get_token_jti(access)
        assert jti is not None and len(jti) > 0

    def test_is_blacklisted_false_by_default(self):
        access, _ = self.manager.create_tokens(USER)
        jti = self.manager.get_token_jti(access)
        assert not self.manager.is_blacklisted(jti)

    def test_is_blacklisted_after_blacklisting_jti(self):
        access, _ = self.manager.create_tokens(USER)
        jti = self.manager.get_token_jti(access)
        self.bl.blacklist_token(jti, timedelta(hours=1))
        assert self.manager.is_blacklisted(jti)

    def test_manager_without_blacklist(self):
        m = TokenManager(HS256, blacklist=None)
        assert not m.is_blacklisted("any-jti")

    def test_is_near_expiry_false_for_fresh_token(self):
        access, _ = self.manager.create_tokens(USER)
        assert not self.manager.is_near_expiry(access)
