"""Coverage tests for stapel_core.core JWT modules.

Targets gaps in:
- core/config.py
- core/jwt_handler.py
- core/token_blacklist.py
- core/token_manager.py
"""
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from django.test import override_settings

from stapel_core.core.config import JWTConfig
from stapel_core.core.jwt_handler import JWTHandler
from stapel_core.core.token_blacklist import TokenBlacklist
from stapel_core.core.token_manager import TokenManager

SECRET = "cov-secret-key-for-testing-purposes-32-bytes"
OTHER_SECRET = "different-secret-key-for-testing-32-bytes!"
USER = {"user_id": "u1", "email": "u@example.com"}


def _cfg(**kwargs):
    defaults = dict(
        secret_key=SECRET,
        algorithm="HS256",
        issuer="cov-iss",
        audience=None,
        access_token_lifetime=timedelta(minutes=5),
        refresh_token_lifetime=timedelta(hours=1),
    )
    defaults.update(kwargs)
    return JWTConfig(**defaults)


# ---------------------------------------------------------------------------
# JWTConfig gaps
# ---------------------------------------------------------------------------

class TestJWTConfigGaps:
    def test_load_keys_from_files(self, tmp_path):
        priv = tmp_path / "priv.pem"
        pub = tmp_path / "pub.pem"
        priv.write_text("PRIVATE-PEM-CONTENT")
        pub.write_text("PUBLIC-PEM-CONTENT")
        cfg = JWTConfig(
            algorithm="RS256",
            private_key_path=str(priv),
            public_key_path=str(pub),
            audience=None,
        )
        assert cfg.private_key == "PRIVATE-PEM-CONTENT"
        assert cfg.public_key == "PUBLIC-PEM-CONTENT"

    def test_load_key_file_missing_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Failed to load key"):
            JWTConfig(
                algorithm="RS256",
                private_key_path=str(tmp_path / "does-not-exist.pem"),
                audience=None,
            )

    def test_rs256_without_any_key_raises(self):
        with pytest.raises(ValueError, match="RS256"):
            JWTConfig(algorithm="RS256", audience=None)

    def test_can_sign_rs256_with_private_key(self):
        cfg = JWTConfig(algorithm="RS256", private_key="PRIV", audience=None)
        assert cfg.can_sign() is True

    def test_can_verify_rs256_public_only(self):
        cfg = JWTConfig(algorithm="RS256", public_key="PUB", audience=None)
        assert cfg.can_verify() is True
        assert cfg.can_sign() is False

    def test_can_verify_rs256_private_only(self):
        cfg = JWTConfig(algorithm="RS256", private_key="PRIV", audience=None)
        assert cfg.can_verify() is True

    def test_get_signing_key_rs256_without_private_raises(self):
        cfg = JWTConfig(algorithm="RS256", public_key="PUB", audience=None)
        with pytest.raises(ValueError, match="private_key"):
            cfg.get_signing_key()

    def test_get_signing_key_rs256_with_private(self):
        cfg = JWTConfig(algorithm="RS256", private_key="PRIV", audience=None)
        assert cfg.get_signing_key() == "PRIV"

    def test_get_verification_key_rs256_prefers_public(self):
        cfg = JWTConfig(
            algorithm="RS256", private_key="PRIV", public_key="PUB", audience=None
        )
        assert cfg.get_verification_key() == "PUB"

    def test_get_verification_key_rs256_falls_back_to_private(self):
        cfg = JWTConfig(algorithm="RS256", private_key="PRIV", audience=None)
        assert cfg.get_verification_key() == "PRIV"

    def test_to_dict_contains_expected_fields(self):
        cfg = _cfg()
        d = cfg.to_dict()
        assert d["secret_key"] == SECRET
        assert d["algorithm"] == "HS256"
        assert d["access_token_lifetime"] == 300.0
        assert d["refresh_token_lifetime"] == 3600.0
        assert d["cookie_name"] == "stapel_jwt"
        assert d["user_identifier_field"] == "user_id"
        assert d["refresh_threshold"] == 300.0

    def test_direct_key_content_wins_over_path(self, tmp_path):
        pub = tmp_path / "pub.pem"
        pub.write_text("FROM-FILE")
        cfg = JWTConfig(
            algorithm="RS256",
            public_key="DIRECT",
            public_key_path=str(pub),
            audience=None,
        )
        assert cfg.public_key == "DIRECT"


# ---------------------------------------------------------------------------
# JWTHandler gaps
# ---------------------------------------------------------------------------

class TestJWTHandlerGaps:
    def test_audience_claim_added_and_verified(self):
        h = JWTHandler(_cfg(audience="stapel"))
        access, refresh = h.generate_token_pair(USER)
        payload = h.decode_token(access)
        assert payload["aud"] == "stapel"
        payload_r = h.decode_token(refresh)
        assert payload_r["aud"] == "stapel"

    def test_wrong_audience_rejected(self):
        signer = JWTHandler(_cfg(audience="stapel"))
        verifier = JWTHandler(_cfg(audience="other-aud"))
        access, _ = signer.generate_token_pair(USER)
        assert verifier.decode_token(access) is None

    def test_missing_audience_rejected_when_required(self):
        signer = JWTHandler(_cfg(audience=None))
        verifier = JWTHandler(_cfg(audience="stapel"))
        access, _ = signer.generate_token_pair(USER)
        assert verifier.decode_token(access) is None

    def test_wrong_issuer_rejected(self):
        signer = JWTHandler(_cfg(issuer="issuer-a"))
        verifier = JWTHandler(_cfg(issuer="issuer-b"))
        access, _ = signer.generate_token_pair(USER)
        assert verifier.decode_token(access) is None

    def test_kid_and_jku_headers_present(self):
        h = JWTHandler(_cfg(key_id="my-kid", jwks_url="https://x/.well-known/jwks.json"))
        access, refresh = h.generate_token_pair(USER)
        for token in (access, refresh):
            header = pyjwt.get_unverified_header(token)
            assert header["kid"] == "my-kid"
            assert header["jku"] == "https://x/.well-known/jwks.json"

    def test_get_key_id_uses_configured_key_id(self):
        h = JWTHandler(_cfg(key_id="explicit-kid"))
        assert h._get_key_id() == "explicit-kid"

    def test_is_token_expired_garbage_token(self):
        h = JWTHandler(_cfg())
        assert h.is_token_expired("not.a.token") is True

    def test_is_token_expired_missing_exp(self):
        h = JWTHandler(_cfg())
        token = pyjwt.encode({"user_id": "u1"}, SECRET, algorithm="HS256")
        assert h.is_token_expired(token) is True

    def test_get_token_expiration_garbage_returns_none(self):
        h = JWTHandler(_cfg())
        assert h.get_token_expiration("garbage") is None

    def test_get_token_expiration_missing_exp_returns_none(self):
        h = JWTHandler(_cfg())
        token = pyjwt.encode({"user_id": "u1"}, SECRET, algorithm="HS256")
        assert h.get_token_expiration(token) is None

    def test_is_token_near_expiry_invalid_token_true(self):
        h = JWTHandler(_cfg())
        assert h.is_token_near_expiry("garbage") is True

    def test_is_token_near_expiry_true_when_close(self):
        h = JWTHandler(
            _cfg(
                access_token_lifetime=timedelta(seconds=30),
                refresh_threshold=timedelta(minutes=5),
            )
        )
        access = h.generate_access_token(USER)
        assert h.is_token_near_expiry(access) is True

    def test_validate_token_type_garbage_false(self):
        h = JWTHandler(_cfg())
        assert h.validate_token_type("garbage", "access") is False

    def test_extract_user_data_empty_payload_returns_none(self):
        h = JWTHandler(_cfg())
        assert h.extract_user_data(None) is None
        assert h.extract_user_data({}) is None

    def test_generate_access_token_without_signing_key_raises(self):
        cfg = JWTConfig(algorithm="RS256", public_key="PUB-ONLY", audience=None)
        h = JWTHandler(cfg)
        with pytest.raises(ValueError, match="private_key"):
            h.generate_access_token(USER)

    def test_generate_refresh_token_without_signing_key_raises(self):
        cfg = JWTConfig(algorithm="RS256", public_key="PUB-ONLY", audience=None)
        h = JWTHandler(cfg)
        with pytest.raises(ValueError, match="private_key"):
            h.generate_refresh_token(USER)

    def test_get_jwks_invalid_public_key_returns_none(self):
        cfg = JWTConfig(algorithm="RS256", public_key="not-a-valid-pem", audience=None)
        h = JWTHandler(cfg)
        assert h.get_jwks() is None

    def test_get_jwks_without_cryptography_returns_none(self):
        import sys

        cfg = JWTConfig(algorithm="RS256", public_key="some-pub-key", audience=None)
        h = JWTHandler(cfg)
        with patch.dict(sys.modules, {"cryptography": None, "cryptography.hazmat": None,
                                      "cryptography.hazmat.primitives": None}):
            assert h.get_jwks() is None

    def test_get_openid_configuration(self):
        h = JWTHandler(_cfg())
        conf = h.get_openid_configuration("https://auth.example.com")
        assert conf["issuer"] == "cov-iss"
        assert conf["jwks_uri"] == "https://auth.example.com/.well-known/jwks.json"
        assert conf["token_endpoint"] == "https://auth.example.com/api/auth/token/"
        assert conf["id_token_signing_alg_values_supported"] == ["HS256"]


# ---------------------------------------------------------------------------
# TokenBlacklist error paths
# ---------------------------------------------------------------------------

class TestTokenBlacklistErrors:
    def _broken_cache(self):
        broken = MagicMock()
        broken.set.side_effect = RuntimeError("cache down")
        broken.get.side_effect = RuntimeError("cache down")
        broken.delete.side_effect = RuntimeError("cache down")
        broken.clear.side_effect = RuntimeError("cache down")
        return broken

    def test_blacklist_token_error_returns_false(self):
        bl = TokenBlacklist(key_prefix="err_bl")
        with patch("django.core.cache.cache", self._broken_cache()):
            assert bl.blacklist_token("jti-x", timedelta(seconds=60)) is False

    def test_is_blacklisted_fails_closed_by_default(self):
        bl = TokenBlacklist(key_prefix="err_bl")
        with patch("django.core.cache.cache", self._broken_cache()):
            assert bl.is_blacklisted("jti-x") is True

    @override_settings(STAPEL_BLACKLIST_FAIL_OPEN=True)
    def test_is_blacklisted_fail_open_when_configured(self):
        bl = TokenBlacklist(key_prefix="err_bl")
        with patch("django.core.cache.cache", self._broken_cache()):
            assert bl.is_blacklisted("jti-x") is False

    def test_remove_from_blacklist_error_returns_false(self):
        bl = TokenBlacklist(key_prefix="err_bl")
        with patch("django.core.cache.cache", self._broken_cache()):
            assert bl.remove_from_blacklist("jti-x") is False

    def test_clear_all_error_returns_false(self):
        bl = TokenBlacklist(key_prefix="err_bl")
        with patch("django.core.cache.cache", self._broken_cache()):
            assert bl.clear_all() is False


# ---------------------------------------------------------------------------
# TokenManager gaps
# ---------------------------------------------------------------------------

class TestTokenManagerGaps:
    def setup_method(self):
        self.signer = TokenManager(_cfg())
        self.other = TokenManager(_cfg(secret_key=OTHER_SECRET))

    def test_validate_access_token_bad_signature_returns_none(self):
        access, _ = self.signer.create_tokens(USER)
        # Token type check passes (unverified) but signature verification fails.
        assert self.other.validate_access_token(access) is None

    def test_validate_refresh_token_bad_signature_returns_none(self):
        _, refresh = self.signer.create_tokens(USER)
        assert self.other.validate_refresh_token(refresh) is None

    def test_refresh_access_token_invalid_refresh_returns_none(self):
        assert self.signer.refresh_access_token("garbage.token") is None

    def test_refresh_access_token_skips_load_without_user_id(self):
        handler = JWTHandler(_cfg())
        refresh = handler.generate_refresh_token({"email": "no-uid@example.com"})
        called = []

        def loader(uid):
            called.append(uid)
            return {"user_id": "should-not-happen"}

        new_access = self.signer.refresh_access_token(refresh, load_user_data=loader)
        assert new_access is not None
        assert called == []

    def test_get_token_jti_garbage_returns_none(self):
        assert self.signer.get_token_jti("garbage") is None

    def test_expired_refresh_token_rejected(self):
        past = int(time.time()) - 100
        expired = pyjwt.encode(
            {"user_id": "u1", "token_type": "refresh", "exp": past, "iss": "cov-iss"},
            SECRET,
            algorithm="HS256",
        )
        assert self.signer.validate_refresh_token(expired) is None
        assert self.signer.refresh_access_token(expired) is None
