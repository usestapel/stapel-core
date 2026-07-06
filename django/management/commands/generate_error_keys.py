"""Emit the ``errors.json`` codegen artifact from the error-key registry.

    python manage.py generate_error_keys --out docs/errors.json

The backend companion of ``schema.json`` / ``flows.json``: a language-agnostic
machine artifact listing every ``error.<status>.<name>`` key the running
instance can raise, with its HTTP ``status``, ``{param}`` interpolation slots,
a machine-readable ``remediation`` hint, and the canonical English text.

Source of truth is the in-process global registry (``register_service_errors``,
the same map ``/error-keys/`` serves) — Django app loading imports every
module's ``errors`` module, so by ``handle`` time the registry is complete. The
output is byte-stable (sorted by code, pinned JSON encoding), so a no-op regen
is a no-op diff — exactly what the drift gate (``test_error_keys.py``) needs.

The array shape mirrors what the frontend ``gen-errors.mjs`` currently produces
by parsing ``errors.py`` directly, so the frontend can migrate onto this
artifact without a format change.
"""
import json
from importlib import import_module
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.module_loading import autodiscover_modules

from stapel_core.django.api.errors import build_error_registry

#: Cross-cutting core mechanisms that own error keys but are not Django apps
#: (so `autodiscover_modules('errors')` does not reach them). Importing the
#: module runs its `register_service_errors(...)` call. Projects add their own
#: non-app error modules via ``settings.STAPEL_ERROR_MODULES``.
_CORE_ERROR_MODULES = (
    "stapel_core.verification.errors",
    "stapel_core.django.captcha",
)


class Command(BaseCommand):
    help = "Emit errors.json (error-key registry) — the backend codegen artifact."

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            default="docs/errors.json",
            help="Output file path (default: docs/errors.json).",
        )

    def handle(self, *args, **options):
        # Populate the registry deterministically instead of relying on import
        # side-effects (a view/serializer happening to have been imported).
        # `<app>.errors` for every INSTALLED_APP that ships one, plus the
        # cross-cutting core mechanisms and any project-declared extras — each
        # module's top-level `register_service_errors(...)` runs on import.
        autodiscover_modules("errors")
        extra = list(_CORE_ERROR_MODULES) + list(
            getattr(settings, "STAPEL_ERROR_MODULES", [])
        )
        for mod in extra:
            try:
                import_module(mod)
            except ImportError:
                continue

        entries = build_error_registry()
        out = Path(options["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        # Byte-stable encoding (mirrors stapel_tools.codegen._stable_json and the
        # frontend's JSON.stringify(…, 2)): 2-space indent, unicode kept readable,
        # single trailing newline. Field order preserved (not sorted) — codes are
        # already sorted inside build_error_registry().
        out.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
        )
        self.stdout.write(
            self.style.SUCCESS(f"wrote {len(entries)} error key(s) to {out}")
        )
