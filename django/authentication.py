"""Backward-compatibility shim — re-exports from stapel_core.django.jwt.authentication."""
from stapel_core.django.jwt.authentication import *  # noqa: F401,F403
from stapel_core.django.jwt.authentication import is_user_blacklisted  # noqa: F401
