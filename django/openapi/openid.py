"""
JWKS and OpenID Connect discovery document generation.

Called from bootstrap.sh during service startup to write static files
served by nginx from /var/www/.well-known/.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from django.conf import settings

WELL_KNOWN_DIR = os.getenv("WELL_KNOWN_DIR", "/var/www/.well-known")


def generate_jwks_to_dir(output_dir: str = WELL_KNOWN_DIR) -> None:
    """
    Write jwks.json and openid-configuration into output_dir.

    For HS256 (dev / no RSA key): skips silently — JWKS is only meaningful
    for asymmetric algorithms where external verifiers need the public key.
    """
    algorithm = getattr(settings, "JWT_ALGORITHM", "HS256")
    issuer = getattr(settings, "JWT_ISSUER", "")
    public_key_pem = getattr(settings, "JWT_PUBLIC_KEY", "")

    if algorithm != "RS256" or not public_key_pem:
        print(f"Skipping JWKS generation (algorithm={algorithm}, public_key={'set' if public_key_pem else 'unset'})")
        return

    os.makedirs(output_dir, exist_ok=True)

    jwks = _build_jwks(public_key_pem)
    openid_config = _build_openid_config(issuer)

    _write_json(os.path.join(output_dir, "jwks.json"), jwks)
    _write_json(os.path.join(output_dir, "openid-configuration"), openid_config)
    print(f"Generated JWKS and OpenID config in {output_dir}")


def _build_jwks(pem: str) -> dict:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        key = serialization.load_pem_public_key(pem.encode(), backend=default_backend())
        nums = key.public_numbers()
        n = nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")
        e = nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")
    except Exception as exc:
        raise RuntimeError(f"Failed to parse RSA public key: {exc}") from exc

    kid = base64.urlsafe_b64encode(hashlib.sha256(pem.encode()).digest()).decode().rstrip("=")[:16]

    return {
        "keys": [{
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": kid,
            "n": _b64url(n),
            "e": _b64url(e),
        }]
    }


def _build_openid_config(issuer: str) -> dict:
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "token_endpoint": f"{issuer}/auth/api/token/",
        "authorization_endpoint": f"{issuer}/auth/api/token/",
        "response_types_supported": ["token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
    }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _write_json(path: str, data: dict) -> None:
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"  wrote {path}")
