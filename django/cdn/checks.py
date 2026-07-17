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
* **E002** — *any* CDN field is declared at all, but no ``cdn.*`` comm
  route is configured — i.e. the cdn module/service was never wired up.
  This is the literal miттudei incident (cdn-modularity.md §0.5): a model
  field frozen to CDN format with no CDN service behind it, caught only
  when a user clicks "Change avatar" in production.
"""
from __future__ import annotations

from django.core import checks

E001_TYPE_NOT_CONFIGURED = "stapel_core.cdn.E001"
E002_CDN_ROUTE_MISSING = "stapel_core.cdn.E002"


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
                f"{field.image_type!r}) объявлен, но {field.image_type!r} "
                f"отсутствует в STAPEL_CDN['ASSET_TYPES'] ({sorted(allowed)}) "
                "— validate()/full_clean() будет падать на каждой попытке "
                "сохранить непустое значение этого поля.",
                hint="Добавьте тип в STAPEL_CDN['ASSET_TYPES'] этого "
                     "проекта (дефолт ('avatar',)) или смените image_type "
                     "поля.",
                id=E001_TYPE_NOT_CONFIGURED,
                obj=field,
            )
        )
    return findings


@checks.register("stapel_cdn")
def check_cdn_module_wired(app_configs=None, **kwargs):
    """E002 — CDN fields exist, but no ``cdn.*`` comm route is configured."""
    fields = list(_iter_cdn_fields())
    if not fields:
        return []

    from stapel_core.comm.exceptions import FunctionRouteNotConfigured
    from stapel_core.comm.functions import _route_for

    try:
        _route_for("cdn.media_exists")
    except FunctionRouteNotConfigured:
        labels = sorted({f"{model._meta.label}.{field.name}" for model, field in fields})
        return [
            checks.Error(
                "CdnImageField/CdnImageListField объявлены (" + ", ".join(labels) +
                "), но cdn.media_exists не имеет настроенного маршрута — "
                "cdn-модуль не подключён, и любая проверка/загрузка через "
                "эти поля упадёт в рантайме на каждой попытке.",
                hint="Подключите stapel-cdn (маршрут STAPEL_COMM для "
                     "cdn.*) или уберите CdnImageField из моделей проекта в "
                     "пользу источника без CDN (например "
                     "stapel_core.media / отдельного source-поля).",
                id=E002_CDN_ROUTE_MISSING,
            )
        ]
    return []


__all__ = [
    "E001_TYPE_NOT_CONFIGURED",
    "E002_CDN_ROUTE_MISSING",
    "check_cdn_field_types_configured",
    "check_cdn_module_wired",
]
