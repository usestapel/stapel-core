"""The CORS credentials rule, in one place.

Audit CDN-01 was raised against an nginx vhost that reflected an arbitrary
``Origin`` with ``Access-Control-Allow-Credentials: true``. The same defect
existed a second time in Python, in this library's shared settings, where it
applied to every service built on them: credentials were asserted
unconditionally while ``CORS_ALLOW_ALL_ORIGINS`` stayed an environment
toggle documented "for local development". One env var was then enough to
reproduce the audited vhost with no nginx involved.

The rule lives here, not inline in ``django/settings.py``, so the settings
module and the boot check that enforces it cannot drift apart — and so it
can be tested directly: importing ``stapel_core.django.settings`` on its own
is impossible (the package pulls in DRF, which demands configured settings),
which is exactly the sort of untestable corner where a security rule rots.
"""
from __future__ import annotations


def derive_allow_credentials(allow_all: bool, allowed_origins) -> bool:
    """Whether cookies may travel cross-origin, given the two origin settings.

    Credentials require a NAMED origin. There is no wildcard that works for a
    credentialed response, so django-cors-headers answers allow-all by echoing
    the caller's own ``Origin`` — which is the vulnerability, not a fallback.
    """
    return bool(allowed_origins) and not allow_all
