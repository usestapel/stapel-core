"""
Iron Common Library - Shared authentication and utilities for Iron microservices.

This package provides framework-agnostic JWT authentication logic and
framework-specific wrappers (Django, Flask, FastAPI) for easy integration.
"""

__version__ = "0.1.0"

# Re-export commonly used components for convenience
from .core.jwt_handler import JWTHandler
from .core.token_manager import TokenManager
from .core.token_blacklist import TokenBlacklist
from .core.config import JWTConfig

__all__ = [
    "JWTHandler",
    "TokenManager",
    "TokenBlacklist",
    "JWTConfig",
]
