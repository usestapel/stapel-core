"""
Configuration module for JWT authentication.

Provides configuration classes that can be used across different frameworks.
Supports both symmetric (HS256) and asymmetric (RS256) algorithms.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import timedelta


# NOTE: the service navigation registry (formerly ``STAPEL_SERVICES`` here) is
# no longer hardcoded in the framework — policy in a mechanism was a bug
# (admin-suite AS-4 §2.2). It is now a deploy-config env-JSON read by
# ``stapel_core.django.nav.get_services`` (written by the project generators).


@dataclass
class JWTConfig:
    """
    JWT configuration settings.

    This is a framework-agnostic configuration class that can be used
    with any Python framework.

    Supports two modes:
    1. Symmetric (HS256): Uses secret_key for both signing and verification
    2. Asymmetric (RS256): Uses private_key for signing, public_key for verification

    For RS256 mode:
    - Auth service: needs private_key (for signing) and optionally public_key
    - Other services: only need public_key (for verification)
    """

    # Secret key for symmetric signing (HS256)
    # For RS256, this can be empty if private_key/public_key are provided
    secret_key: str = ""

    # RSA keys for asymmetric signing (RS256)
    # private_key: PEM-encoded private key (only needed for token generation)
    # public_key: PEM-encoded public key (needed for token verification)
    private_key: Optional[str] = None
    public_key: Optional[str] = None

    # Path to key files (alternative to providing key content directly)
    private_key_path: Optional[str] = None
    public_key_path: Optional[str] = None

    # Standard JWT claims
    issuer: str = "stapel-auth"  # iss claim - identifies the token issuer
    audience: Optional[str] = "stapel"  # aud claim - intended audience
    key_id: Optional[str] = None  # kid header - identifies which key was used
    jwks_url: Optional[str] = None  # jku header - URL to JWKS for key discovery

    # Token lifetimes
    access_token_lifetime: timedelta = field(default_factory=lambda: timedelta(hours=1))
    refresh_token_lifetime: timedelta = field(default_factory=lambda: timedelta(days=7))

    # Algorithm for signing: "HS256" (symmetric) or "RS256" (asymmetric)
    algorithm: str = "HS256"

    # Cookie settings
    cookie_name: str = "stapel_jwt"
    refresh_cookie_name: str = "stapel_refresh_jwt"
    cookie_path: str = "/"
    cookie_domain: Optional[str] = None
    cookie_secure: bool = False  # Set to True in production with HTTPS
    cookie_httponly: bool = True
    cookie_samesite: str = "Lax"  # "Strict", "Lax", or "None"

    # Token settings
    token_type: str = "Bearer"

    # Header settings
    auth_header_name: str = "Authorization"
    auth_header_prefix: str = "Bearer"

    # User identifier field (used for matching users across services)
    user_identifier_field: str = "user_id"

    # Refresh threshold - token is considered "near expiry" if less than this time remains
    refresh_threshold: timedelta = field(default_factory=lambda: timedelta(minutes=5))

    def __post_init__(self):
        """Validate configuration after initialization."""
        # Load keys from files if paths are provided
        if self.private_key_path and not self.private_key:
            self.private_key = self._load_key_file(self.private_key_path)

        if self.public_key_path and not self.public_key:
            self.public_key = self._load_key_file(self.public_key_path)

        # Validate key configuration based on algorithm
        if self.algorithm == "RS256":
            # RS256 requires at least public_key for verification
            if not self.public_key and not self.private_key:
                raise ValueError("RS256 algorithm requires either public_key or private_key")
        elif self.algorithm == "HS256":
            if not self.secret_key:
                raise ValueError("HS256 algorithm requires secret_key")
        else:
            raise ValueError(f"Unsupported algorithm: {self.algorithm}. Use 'HS256' or 'RS256'")

        if self.access_token_lifetime >= self.refresh_token_lifetime:
            raise ValueError("refresh_token_lifetime must be greater than access_token_lifetime")

        if self.cookie_samesite not in ["Strict", "Lax", "None"]:
            raise ValueError("cookie_samesite must be 'Strict', 'Lax', or 'None'")

    def _load_key_file(self, path: str) -> str:
        """Load a key from a file path."""
        try:
            with open(path, 'r') as f:
                return f.read()
        except Exception as e:
            raise ValueError(f"Failed to load key from {path}: {e}")

    def can_sign(self) -> bool:
        """Check if this config can sign tokens (generate new tokens)."""
        if self.algorithm == "RS256":
            return self.private_key is not None
        return bool(self.secret_key)

    def can_verify(self) -> bool:
        """Check if this config can verify tokens."""
        if self.algorithm == "RS256":
            return self.public_key is not None or self.private_key is not None
        return bool(self.secret_key)

    def get_signing_key(self):
        """Get the key used for signing tokens."""
        if self.algorithm == "RS256":
            if not self.private_key:
                raise ValueError("RS256 signing requires private_key")
            return self.private_key
        return self.secret_key

    def get_verification_key(self):
        """Get the key used for verifying tokens."""
        if self.algorithm == "RS256":
            # Prefer public key, but private key can also verify
            return self.public_key or self.private_key
        return self.secret_key

    def to_dict(self) -> dict:
        """Convert config to dictionary."""
        return {
            "secret_key": self.secret_key,
            "access_token_lifetime": self.access_token_lifetime.total_seconds(),
            "refresh_token_lifetime": self.refresh_token_lifetime.total_seconds(),
            "algorithm": self.algorithm,
            "cookie_name": self.cookie_name,
            "refresh_cookie_name": self.refresh_cookie_name,
            "cookie_path": self.cookie_path,
            "cookie_domain": self.cookie_domain,
            "cookie_secure": self.cookie_secure,
            "cookie_httponly": self.cookie_httponly,
            "cookie_samesite": self.cookie_samesite,
            "token_type": self.token_type,
            "auth_header_name": self.auth_header_name,
            "auth_header_prefix": self.auth_header_prefix,
            "user_identifier_field": self.user_identifier_field,
            "refresh_threshold": self.refresh_threshold.total_seconds(),
        }
