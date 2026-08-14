"""System checks for ``stapel_core.django.cdn`` (tag ``stapel_cdn``).

E-level — declaring ``CdnImageField``/``CdnImageListField`` is cheap; two
things are easy to get wrong and previously failed only deep in production,
per-request, instead of at ``manage.py check`` / boot-smoke time
(cdn-modularity.md §0.1, the "half the stack is modular, half isn't, and
nothing catches the mismatch" finding):

* **E001** — the field's ``image_type`` is not in this deployment's
  ``STAPEL_CDN["ASSET_TYPES"]``. Before 0.12.4 this either raised
  ``ValueError`` at class-definition time against a frozen 6-value enum
  (the legacy marketplace behavior — impossible for a host project to add
  its own type) or, after the freeze was lifted, would only surface as a
  ``ValidationError`` inside ``full_clean()`` — a path plenty of DRF
  viewsets skip.
* **E002** — *any* CDN field is declared at all, but ``cdn.media_exists``
  is not reachable over this deployment's comm transport — i.e. the cdn
  module/service was never wired up. This is the literal meettoday incident
  (cdn-modularity.md §0.5): a model field frozen to CDN format with no CDN
  service behind it, caught only when a user clicks "Change avatar" in
  production.

  What "reachable" means is the transport's answer, not the route table's:
  ``FUNCTION_ROUTES`` is http-only, so reading it under NATS (where the
  subject IS the function name) reported a correctly wired fleet as unwired.
  ``comm.function_unreachable_reason`` asks each transport the question it
  can actually answer.
"""
from __future__ import annotations

from django.core import checks

E001_TYPE_NOT_CONFIGURED = "stapel_core.cdn.E001"
E002_CDN_ROUTE_MISSING = "stapel_core.cdn.E002"

CDN_MEDIA_EXISTS = "cdn.media_exists"


def _iter_cdn_fields():
    from django.apps import apps

    from .fields import CdnImageField, CdnImageListField

    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, (CdnImageField, CdnImageListField)):
                yield model, field


@checks.register("stapel_cdn")
def check_cdn_field_types_configured(app_configs=None, **kwargs):
    """E001 — a declared ``image_type`` is missing from ``ASSET_TYPES``."""
    from .conf import cdn_settings

    allowed = set(cdn_settings.ASSET_TYPES)
    findings = []
    for model, field in _iter_cdn_fields():
        if field.image_type in allowed:
            continue
        findings.append(
            checks.Error(
                f"{model._meta.label}.{field.name}: CdnImageField(image_type="
                f"{field.image_type!r}) is declared, but {field.image_type!r} "
                f"is missing from STAPEL_CDN['ASSET_TYPES'] ({sorted(allowed)}) "
                "— validate()/full_clean() will fail on every attempt to "
                "save a non-empty value for this field.",
                hint="Add the type to this project's STAPEL_CDN['ASSET_TYPES'] "
                     "(default ('avatar',)) or change the field's image_type.",
                id=E001_TYPE_NOT_CONFIGURED,
                obj=field,
            )
        )
    return findings


@checks.register("stapel_cdn")
def check_cdn_module_wired(app_configs=None, **kwargs):
    """E002 — CDN fields exist, but ``cdn.media_exists`` is unreachable."""
    fields = list(_iter_cdn_fields())
    if not fields:
        return []

    from stapel_core.comm.functions import function_unreachable_reason

    reason = function_unreachable_reason(CDN_MEDIA_EXISTS)
    if reason is None:
        return []
    labels = sorted({f"{model._meta.label}.{field.name}" for model, field in fields})
    return [
        checks.Error(
            "CdnImageField/CdnImageListField are declared (" + ", ".join(labels) +
            f"), but {CDN_MEDIA_EXISTS} is not reachable in this deployment "
            f"({reason}) — the cdn module is not wired up, and any "
            "check/upload through these fields will fail at runtime on every "
            "attempt.",
            hint="Wire up stapel-cdn for the transport this deployment runs — "
                 "install it in this process (inprocess), add a "
                 "STAPEL_COMM['FUNCTION_ROUTES'] entry for 'cdn.' (http), or "
                 "run the cdn service's `manage.py serve_functions` (nats) — "
                 "or remove CdnImageField from the project's models in favor "
                 "of a source without CDN (e.g. stapel_core.media / a "
                 "separate source field).",
            id=E002_CDN_ROUTE_MISSING,
        )
    ]


__all__ = [
    "CDN_MEDIA_EXISTS",
    "E001_TYPE_NOT_CONFIGURED",
    "E002_CDN_ROUTE_MISSING",
    "check_cdn_field_types_configured",
    "check_cdn_module_wired",
]
