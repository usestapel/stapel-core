"""``stapel_core.django.admin`` — admin visibility & navigation helpers.

This package is also the autodiscovered "admin module" of the
``common_django`` app, so imports here must stay side-effect free —
registry mutations live in :mod:`.registration` and run from
``CommonDjangoConfig.ready()``.
"""
from .base import MASK_PLACEHOLDER, SECRET_FIELD_PATTERNS, StapelModelAdmin
from .conf import admin_settings

# Navigation registry (admin-suite AS-4) — re-exported here so a module
# registers its dashboard with the ergonomic
# ``from stapel_core.django.admin import register_nav_link`` (§2.3).
from stapel_core.django.nav import register_nav_link, unregister_nav_link

__all__ = [
    "MASK_PLACEHOLDER",
    "SECRET_FIELD_PATTERNS",
    "StapelModelAdmin",
    "admin_settings",
    "register_nav_link",
    "unregister_nav_link",
]
