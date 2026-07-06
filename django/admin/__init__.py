"""``stapel_core.django.admin`` — admin visibility & navigation helpers.

This package is also the autodiscovered "admin module" of the
``common_django`` app, so imports here must stay side-effect free —
registry mutations live in :mod:`.registration` and run from
``CommonDjangoConfig.ready()``.
"""
from .base import MASK_PLACEHOLDER, SECRET_FIELD_PATTERNS, StapelModelAdmin
from .conf import admin_settings

__all__ = [
    "MASK_PLACEHOLDER",
    "SECRET_FIELD_PATTERNS",
    "StapelModelAdmin",
    "admin_settings",
]
