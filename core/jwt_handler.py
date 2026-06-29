"""
Framework-agnostic JWT handler.

Provides pure Python implementation of JWT token generation, validation,
and decoding without any framework-specific dependencies.

Supports both symmetric (HS256) and asymmetric (RS256) algorithms:
- HS256: Uses shared secret_key for signing and verification
- RS256: Uses private_key for signing, public_key for verification

For RS256 mode in a microservices architecture:
- Auth service: Has private_key (for signing) and public_key
- Other services: Only have public_key (for verification only)
"""

import jwt
import logging
import hashlib
import base64
import time as _time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

from .config import JWTConfig


logger = logging.getLogger(__name__)


class JWTHandler:
    """
    Pure Python JWT handler for generating and validating tokens.

    This class has no framework dependencies and can be used with
    Django, Flask, FastAPI, or any other Python framework.

    Supports both symmetric (HS256) and asymmetric (RS256) algorithms:
    - HS256: Uses shared secret_key for both signing and verification
    - RS256: Uses RSA key pair (private for signing, public for verification)

    In RS256 mode, token generation requires private_key. Services without
    private_key can only validate tokens, not generate them.
    """

    def __init__(self, config: JWTConfig):
        """
        Initialize JWT handler with configuration.

        Args:
            config: JWTConfig instance with JWT settings
        """
        self.config = config

    def generate_token_pair(self, user_data: Dict[str, Any]) -> Tuple[str, str]:
        """
        Generate access and refresh token pair.

        Args:
            user_data: Dictionary containing user information to encode in tokens.
                      Should include user_identifier_field (e.g., 'email')

        Returns:
            Tuple of (access_token, refresh_token)

        Raises:
            ValueError: If required user data is missing or signing not available
        """
        if not self.config.can_sign():
            raise ValueError(
                f"Token generation not available. "
                f"For RS256, private_key is required. "
                f"Current algorithm: {self.config.algorithm}"
            )

        if self.config.user_identifier_field not in user_data:
            raise ValueError(f"user_data must contain '{self.config.user_identifier_field}'")

        access_token = self.generate_access_token(user_data)
        refresh_token = self.generate_refresh_token(user_data)

        return access_token, refresh_token

    def generate_access_token(self, user_data: Dict[str, Any]) -> str:
        """
        Generate an access token.

        Args:
            user_data: Dictionary containing user information

        Returns:
            Encoded JWT access token string

        Raises:
            ValueError: If signing is not available (RS256 without private_key)
        """
        if not self.config.can_sign():
            raise ValueError(
                "Token generation not available. "
                "For RS256, private_key is required."
            )

        now = datetime.now(timezone.utc)
        payload = {
            **user_data,
            "token_type": "access",
            "exp": now + self.config.access_token_lifetime,
            "iat": now,
            "jti": self._generate_jti(),
            "iss": self.config.issuer,
        }

        # Add audience if configured
        if self.config.audience:
            payload["aud"] = self.config.audience

        # Build headers with kid and jku if available
        headers = {}
        kid = self._get_key_id()
        if kid:
            headers["kid"] = kid
        if self.config.jwks_url:
            headers["jku"] = self.config.jwks_url

        signing_key = self.config.get_signing_key()
        return jwt.encode(payload, signing_key, algorithm=self.config.algorithm, headers=headers if headers else None)

    def generate_refresh_token(self, user_data: Dict[str, Any]) -> str:
        """
        Generate a refresh token.

        Args:
            user_data: Dictionary containing user information

        Returns:
            Encoded JWT refresh token string

        Raises:
            ValueError: If signing is not available (RS256 without private_key)
        """
        if not self.config.can_sign():
            raise ValueError(
                "Token generation not available. "
                "For RS256, private_key is required."
            )

        now = datetime.now(timezone.utc)
        # Include all user data in refresh token so it can be used for token refresh
        # without needing to query database (important for cross-service auth)
        payload = {
            **user_data,
            "token_type": "refresh",
            "exp": now + self.config.refresh_token_lifetime,
            "iat": now,
            "jti": self._generate_jti(),
            "iss": self.config.issuer,
        }

        # Add audience if configured
        if self.config.audience:
            payload["aud"] = self.config.audience

        # Build headers with kid and jku if available
        headers = {}
        kid = self._get_key_id()
        if kid:
            headers["kid"] = kid
        if self.config.jwks_url:
            headers["jku"] = self.config.jwks_url

        signing_key = self.config.get_signing_key()
        return jwt.encode(payload, signing_key, algorithm=self.config.algorithm, headers=headers if headers else None)

    def decode_token(self, token: str, verify: bool = True) -> Optional[Dict[str, Any]]:
        """
        Decode and validate a JWT token.

        Args:
            token: JWT token string
            verify: Whether to verify token signature and expiration

        Returns:
            Decoded token payload as dictionary, or None if invalid
        """
        try:
            options = {
                "verify_signature": verify,
                "verify_exp": verify,
                "verify_iss": verify and bool(self.config.issuer),
                "verify_aud": verify and bool(self.config.audience),
            }

            # Get the appropriate key for verification
            if verify:
                verification_key = self.config.get_verification_key()
            else:
                # When not verifying, key doesn't matter but PyJWT still needs one
                verification_key = self.config.get_verification_key() or ""

            # Build decode kwargs
            decode_kwargs: Dict[str, Any] = {
                "algorithms": [self.config.algorithm],
                "options": options,
            }

            # Add issuer verification if configured
            if verify and self.config.issuer:
                decode_kwargs["issuer"] = self.config.issuer

            # Add audience verification if configured
            if verify and self.config.audience:
                decode_kwargs["audience"] = self.config.audience

            payload = jwt.decode(
                token,
                verification_key,
                **decode_kwargs
            )
            return payload
        except jwt.ExpiredSignatureError:
            logger.warning("[DEBUG] Token has expired")
            return None
        except jwt.InvalidIssuerError as e:
            logger.warning(f"[DEBUG] Invalid token issuer: {e}")
            return None
        except jwt.InvalidAudienceError as e:
            logger.warning(f"[DEBUG] Invalid token audience: {e}")
            return None
        except jwt.InvalidTokenError as e:
            # Log detailed token info for debugging
            try:
                header = jwt.get_unverified_header(token)
                # Also try to decode payload without verification
                import base64
                parts = token.split('.')
                if len(parts) >= 2:
                    # Decode payload (add padding if needed)
                    payload_b64 = parts[1] + '=' * (4 - len(parts[1]) % 4)
                    payload_raw = base64.urlsafe_b64decode(payload_b64).decode('utf-8')
                    import json
                    payload_data = json.loads(payload_raw)
                    exp = payload_data.get('exp', 'N/A')
                    iat = payload_data.get('iat', 'N/A')
                    user_id = payload_data.get('user_id', 'N/A')
                    token_type = payload_data.get('token_type', 'N/A')
                    logger.warning(f"[DEBUG] Invalid token: {e} | alg={header.get('alg')} expected={self.config.algorithm} | type={token_type} user={user_id} exp={exp} iat={iat}")
                else:
                    logger.warning(f"[DEBUG] Invalid token: {e} | header={header}")
            except Exception as parse_err:
                logger.warning(f"[DEBUG] Invalid token: {e} | parse_error={parse_err}")
            return None

    def is_token_expired(self, token: str) -> bool:
        """
        Check if token is expired without raising exception.

        Args:
            token: JWT token string

        Returns:
            True if token is expired, False otherwise
        """
        payload = self.decode_token(token, verify=False)
        if not payload:
            return True

        exp = payload.get("exp")
        if not exp:
            return True

        return _time.time() >= exp

    def get_token_expiration(self, token: str) -> Optional[datetime]:
        """
        Get token expiration datetime.

        Args:
            token: JWT token string

        Returns:
            Expiration datetime or None if token is invalid
        """
        payload = self.decode_token(token, verify=False)
        if not payload:
            return None

        exp = payload.get("exp")
        if not exp:
            return None

        return datetime.fromtimestamp(exp, tz=timezone.utc)

    def is_token_near_expiry(self, token: str) -> bool:
        """
        Check if token is near expiration (within refresh threshold).

        Args:
            token: JWT token string

        Returns:
            True if token should be refreshed, False otherwise
        """
        expiration = self.get_token_expiration(token)
        if not expiration:
            return True

        time_until_expiry = expiration - datetime.now(timezone.utc)
        return time_until_expiry <= self.config.refresh_threshold

    def validate_token_type(self, token: str, expected_type: str) -> bool:
        """
        Validate that token is of expected type (access or refresh).

        Args:
            token: JWT token string
            expected_type: Expected token type ("access" or "refresh")

        Returns:
            True if token type matches, False otherwise
        """
        payload = self.decode_token(token, verify=False)
        if not payload:
            return False

        return payload.get("token_type") == expected_type

    def extract_user_data(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract user data from token payload.

        Args:
            token: JWT token string

        Returns:
            Dictionary with user data or None if token is invalid
        """
        if not payload:
            return None

        # Remove JWT-specific claims
        user_data = {k: v for k, v in payload.items()
                    if k not in ["exp", "iat", "jti", "token_type"]}

        return user_data

    @staticmethod
    def _generate_jti() -> str:
        """
        Generate unique token identifier (jti).

        Returns:
            Unique token identifier string
        """
        import uuid
        return str(uuid.uuid4())

    def _get_key_id(self) -> Optional[str]:
        """
        Get key ID (kid) for JWT header.

        For RS256, generates kid from public key hash.
        For HS256, uses configured key_id or None.

        Returns:
            Key ID string or None
        """
        # Use explicitly configured key_id if available
        if self.config.key_id:
            return self.config.key_id

        # For RS256, generate kid from public key thumbprint
        if self.config.algorithm == "RS256" and self.config.public_key:
            return self._compute_key_thumbprint(self.config.public_key)

        return None

    @staticmethod
    def _compute_key_thumbprint(public_key_pem: str) -> str:
        """
        Compute SHA-256 thumbprint of public key for use as kid.

        Args:
            public_key_pem: PEM-encoded public key

        Returns:
            Base64url-encoded SHA-256 hash (first 16 chars for brevity)
        """
        # Hash the public key content
        key_hash = hashlib.sha256(public_key_pem.encode()).digest()
        # Base64url encode (URL-safe, no padding)
        thumbprint = base64.urlsafe_b64encode(key_hash).decode().rstrip('=')
        # Return first 16 chars for brevity
        return thumbprint[:16]

    def get_jwks(self) -> Optional[Dict[str, Any]]:
        """
        Generate JWKS (JSON Web Key Set) for public key verification.

        Only available for RS256 algorithm with public key.
        This allows other services to verify tokens without sharing private key.

        Returns:
            JWKS dictionary or None if not available
        """
        if self.config.algorithm != "RS256" or not self.config.public_key:
            return None

        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.backends import default_backend

            # Load the public key
            public_key = serialization.load_pem_public_key(
                self.config.public_key.encode(),
                backend=default_backend()
            )

            # Get the public numbers
            public_numbers = public_key.public_numbers()

            # Convert to bytes and base64url encode
            def int_to_base64url(n: int, length: int) -> str:
                data = n.to_bytes(length, byteorder='big')
                return base64.urlsafe_b64encode(data).decode().rstrip('=')

            # RSA key size in bytes
            key_size = (public_numbers.n.bit_length() + 7) // 8

            kid = self._get_key_id() or "default"

            jwk = {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": int_to_base64url(public_numbers.n, key_size),
                "e": int_to_base64url(public_numbers.e, 3),  # e is typically 65537 (3 bytes)
            }

            return {
                "keys": [jwk]
            }
        except ImportError:
            logger.warning("cryptography library required for JWKS generation")
            return None
        except Exception as e:
            logger.error(f"Failed to generate JWKS: {e}")
            return None

    def get_openid_configuration(self, base_url: str) -> Dict[str, Any]:
        """
        Generate OpenID Connect discovery document.

        Args:
            base_url: Base URL of the auth service (e.g., https://auth.iron.com)

        Returns:
            OpenID Configuration dictionary
        """
        config = {
            "issuer": self.config.issuer,
            "jwks_uri": f"{base_url}/.well-known/jwks.json",
            "token_endpoint": f"{base_url}/api/auth/token/",
            "response_types_supported": ["token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": [self.config.algorithm],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
        }
        return config
