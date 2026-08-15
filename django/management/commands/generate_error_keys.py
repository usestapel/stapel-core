"""Emit the ``errors.json`` codegen artifact from the error-key registry.

    python manage.py generate_error_keys --out docs/errors.json

The backend companion of ``schema.json`` / ``flows.json``: a language-agnostic
machine artifact listing every ``error.<status>.<name>`` key the running
instance can raise, with its HTTP ``status``, ``{param}`` interpolation slots,
a machine-readable ``remediation`` hint, the canonical English text, and the
``owner`` package whose ``translations/errors.<lang>.json`` carry the key.

The registry is instance-scoped on purpose (a deployment's codes, like its
schema paths); ``owner`` is what tells a consumer where each code's catalogs
live. Emission refuses to write an artifact whose declared codes an owner's
shipped language does not translate (``check_registry_catalog_pairing``) — the
two halves of the contract cannot drift apart silently.

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
from django.core.management.base import BaseCommand, CommandError
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

        # Pairing gate: the artifact declares codes, the owners' catalogs
        # carry their translations — two halves of one contract. Refuse to
        # emit a registry whose declared codes an owner's shipped language
        # does not cover; the drift gates re-run this command in CI, so the
        # break is red in every emitting repo, not silent at a user's screen.
        from stapel_core.i18n import check_registry_catalog_pairing, summarize

        issues = check_registry_catalog_pairing(entries)
        for issue in issues:
            style = self.style.ERROR if issue.level == "error" else self.style.WARNING
            self.stderr.write(style(f"[{issue.level}:{issue.code}] {issue.message}"))
        n_errors, _ = summarize(issues)
        if n_errors:
            raise CommandError(
                f"{n_errors} declared error code(s) missing from their owner's "
                f"shipped catalogs — errors.json not written"
            )

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
