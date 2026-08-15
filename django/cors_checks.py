"""System checks for the CORS pair (tag ``stapel_cors``).

The 2026-08-11 audit raised CDN-01: an nginx vhost reflected an arbitrary
``Origin`` back with ``Access-Control-Allow-Credentials: true``, which lets
any site a victim visits read authenticated responses cross-origin. Fixing
the vhost alone would have closed one instance of the defect and left a
second, wider one standing: **this library reproduces it in Python for every
service that uses these settings.**

``django/settings.py`` used to set ``CORS_ALLOW_CREDENTIALS = True``
unconditionally while exposing ``CORS_ALLOW_ALL_ORIGINS`` as an environment
toggle "for local development". django-cors-headers, given both, echoes the
request's own ``Origin`` verbatim instead of ``*`` (there is no wildcard that
works with credentials) — so one leaked or copy-pasted env var turns every
service into the audited vhost, with no nginx involved.

The pair is refused at boot rather than silently downgraded: a deployment
that asked for allow-all AND credentials has stated two incompatible
intentions, and picking one for it would leave the operator believing the
other. E-level, because the failure is a silent cross-origin read of
authenticated data.
"""
from __future__ import annotations

from django.core import checks

from stapel_core.django.check_guard import (
    SecurityCriticalError,
    declare_security_critical,
)
from stapel_core.security.cors import derive_allow_credentials  # noqa: F401

#: The id IS its security-critical declaration, so no blanket
#: SILENCED_SYSTEM_CHECKS line can mute it (stapel_core.django.check_guard).
E001_CREDENTIALS_WITH_ALL_ORIGINS = declare_security_critical(
    "stapel_core.cors.E001",
    "allow-all origins with credentials lets any site the user visits read "
    "authenticated responses from this service",
)
W002_CREDENTIALS_WITHOUT_ALLOWLIST = "stapel_core.cors.W002"


@checks.register("stapel_cors")
def check_cors_credentials(app_configs=None, **kwargs):
    from django.conf import settings

    allow_all = bool(getattr(settings, "CORS_ALLOW_ALL_ORIGINS", False))
    credentials = bool(getattr(settings, "CORS_ALLOW_CREDENTIALS", False))
    allowlist = list(getattr(settings, "CORS_ALLOWED_ORIGINS", ()) or ())
    regexes = list(getattr(settings, "CORS_ALLOWED_ORIGIN_REGEXES", ()) or ())

    errors = []
    if allow_all and credentials:
        errors.append(SecurityCriticalError(
            "CORS_ALLOW_ALL_ORIGINS and CORS_ALLOW_CREDENTIALS are both on. "
            "django-cors-headers cannot answer that with a wildcard, so it "
            "reflects the caller's own Origin and marks the response "
            "credentialed: any site the user visits can read authenticated "
            "responses from this service.",
            hint="Keep CORS_ALLOW_ALL_ORIGINS for local development only, "
                 "and only with CORS_ALLOW_CREDENTIALS off. For a deployment "
                 "that needs cookies cross-origin, list the exact origins in "
                 "CORS_ALLOWED_ORIGINS (or CORS_ALLOWED_ORIGIN_REGEXES).",
            id=E001_CREDENTIALS_WITH_ALL_ORIGINS,
        ))

    if credentials and not allow_all and not allowlist and not regexes:
        errors.append(checks.Warning(
            "CORS_ALLOW_CREDENTIALS is on but no origin is allowed, so no "
            "cross-origin caller can use it.",
            hint="Either list the origins that need it in "
                 "CORS_ALLOWED_ORIGINS, or turn CORS_ALLOW_CREDENTIALS off "
                 "so the intent is not carried by a setting nothing reads.",
            id=W002_CREDENTIALS_WITHOUT_ALLOWLIST,
        ))
    return errors
