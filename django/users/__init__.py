"""Core's user model app, and the one seam that materialises a shadow row."""

from .shadow import ensure_shadow_user

__all__ = ["ensure_shadow_user"]
