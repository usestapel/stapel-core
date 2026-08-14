"""Tests for stapel_core.django.cdn.conf and .checks (tag ``stapel_cdn``).

Covers the two owner-visible failure modes from cdn-modularity.md:

1. A declared ``CdnImageField``'s ``image_type`` is missing from this
   deployment's ``STAPEL_CDN["ASSET_TYPES"]`` (E001).
2. Any ``CdnImageField``/``CdnImageListField`` is declared, but no
   ``cdn.*`` comm route is configured at all — the meettoday incident (E002).
"""
from __future__ import annotations

import pytest
from django.db import models

from stapel_core.django.cdn.checks import (
    CDN_MEDIA_EXISTS,
    E001_TYPE_NOT_CONFIGURED,
    E002_CDN_ROUTE_MISSING,
    check_cdn_field_types_configured,
    check_cdn_module_wired,
)
from stapel_core.django.cdn.conf import DEFAULT_ASSET_TYPES, cdn_settings
from stapel_core.django.cdn.fields import CdnImageField, CdnImageListField


@pytest.fixture(autouse=True)
def _reset_cdn_settings_cache():
    cdn_settings.reload()
    yield
    cdn_settings.reload()


# ---------------------------------------------------------------------------
# conf.py
# ---------------------------------------------------------------------------


def test_default_asset_types_is_avatar_only():
    assert DEFAULT_ASSET_TYPES == ("avatar",)
    assert cdn_settings.ASSET_TYPES == ("avatar",)


def test_asset_types_overridable_via_settings(settings):
    settings.STAPEL_CDN = {"ASSET_TYPES": ("avatar", "banner")}
    cdn_settings.reload()
    assert cdn_settings.ASSET_TYPES == ("avatar", "banner")


# ---------------------------------------------------------------------------
# checks.py — E001 (type not configured)
# ---------------------------------------------------------------------------

# A private, distinct app_label from the CdnCovThing model in
# test_cov_infra_cdn.py so both modules' models.get_models() don't collide.


class CdnChecksThing(models.Model):
    avatar = CdnImageField(image_type="avatar", blank=True, null=True)
    banner = CdnImageField(image_type="banner", blank=True, null=True)
    gallery = CdnImageListField(image_type="unconfigured_type", null=True)

    class Meta:
        app_label = "users"


def test_e001_clean_when_type_in_default_asset_types():
    errors = check_cdn_field_types_configured()
    flagged_fields = {e.obj.name for e in errors if e.obj is not None}
    # 'avatar' is in the zero-config default — never flagged.
    assert "avatar" not in flagged_fields
    assert E001_TYPE_NOT_CONFIGURED in {e.id for e in errors}  # banner/gallery still unconfigured


def test_e001_flags_unconfigured_types():
    errors = check_cdn_field_types_configured()
    flagged_fields = {
        e.obj.name for e in errors if e.id == E001_TYPE_NOT_CONFIGURED and e.obj is not None
    }
    assert "banner" in flagged_fields
    assert "gallery" in flagged_fields
    assert "avatar" not in flagged_fields


def test_e001_clean_once_type_added_to_settings(settings):
    settings.STAPEL_CDN = {"ASSET_TYPES": ("avatar", "banner", "unconfigured_type")}
    cdn_settings.reload()
    errors = check_cdn_field_types_configured()
    # No more E001s for *this* model's fields, regardless of what other
    # test modules' models declare (apps.get_models() is process-wide).
    flagged_fields = {
        e.obj.name for e in errors if e.obj is not None and e.obj.model is CdnChecksThing
    }
    assert flagged_fields == set()


# ---------------------------------------------------------------------------
# checks.py — E002 (cdn module not wired)
# ---------------------------------------------------------------------------


def test_e002_noop_when_no_cdn_fields_declared(monkeypatch):
    monkeypatch.setattr(
        "stapel_core.django.cdn.checks._iter_cdn_fields", lambda: iter(())
    )
    assert check_cdn_module_wired() == []


def test_e002_flags_missing_route_when_fields_exist():
    # Default transport (inprocess) and no cdn provider registered anywhere in
    # the test settings (tests/conftest.py doesn't wire one) — this is the
    # meettoday scenario.
    errors = check_cdn_module_wired()
    assert len(errors) == 1
    assert errors[0].id == E002_CDN_ROUTE_MISSING
    assert "CdnChecksThing" in errors[0].msg


def test_e002_clean_when_route_configured(settings):
    settings.STAPEL_COMM = {
        "FUNCTION_TRANSPORT": "http",
        "FUNCTION_ROUTES": {"cdn.": "http://stapel-cdn:8000/cdn"},
    }
    assert check_cdn_module_wired() == []


# E002 asks the transport, not the route table. FUNCTION_ROUTES is http-only
# (comm/config.py), so consulting it under NATS — where the subject IS the
# function name — refused a correctly wired fleet. With the 0.25.0 boot gate
# that would have been a refusal to start, not a noisy `manage.py check`.


def test_e002_silent_under_nats_with_no_routes(settings):
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "nats"}
    assert check_cdn_module_wired() == []


def test_e002_still_errors_under_http_with_no_route(settings):
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "http"}
    errors = check_cdn_module_wired()
    assert [e.id for e in errors] == [E002_CDN_ROUTE_MISSING]
    assert "FUNCTION_ROUTES" in errors[0].msg


def test_e002_silent_inprocess_with_a_registered_provider(settings):
    from stapel_core.comm import function_registry, register_function

    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "inprocess"}
    register_function(CDN_MEDIA_EXISTS, lambda payload: {"exists": True})
    try:
        assert check_cdn_module_wired() == []
    finally:
        function_registry.clear()


def test_e002_errors_inprocess_without_a_provider(settings):
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "inprocess"}
    errors = check_cdn_module_wired()
    assert [e.id for e in errors] == [E002_CDN_ROUTE_MISSING]
    assert "inprocess" in errors[0].msg


def test_e002_errors_on_a_transport_comm_cannot_dispatch(settings):
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "carrierpigeon"}
    errors = check_cdn_module_wired()
    assert [e.id for e in errors] == [E002_CDN_ROUTE_MISSING]
    assert "FUNCTION_TRANSPORT" in errors[0].msg


def test_e002_silent_under_a_custom_dotted_transport(settings):
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "acme.rpc.transport"}
    assert check_cdn_module_wired() == []


# ---------------------------------------------------------------------------
# End to end, through Django's real check registry
# ---------------------------------------------------------------------------


def test_stapel_cdn_tag_is_clean_under_nats(settings):
    """`run_checks(tags=['stapel_cdn'])` — the path the boot gate would take.

    Hand-calling the check function proves the function; this proves the
    registration. Every declared image_type is admitted so an E001 from a
    neighbouring test module's model cannot mask the E002 question.
    """
    from django.core import checks as django_checks

    from stapel_core.django.cdn.checks import _iter_cdn_fields

    settings.STAPEL_CDN = {
        "ASSET_TYPES": tuple(sorted({f.image_type for _, f in _iter_cdn_fields()}))
    }
    cdn_settings.reload()

    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "http"}
    before = django_checks.run_checks(tags=["stapel_cdn"])
    assert [f.id for f in before] == [E002_CDN_ROUTE_MISSING]

    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "nats"}
    assert django_checks.run_checks(tags=["stapel_cdn"]) == []


def test_a_nats_deployment_declaring_cdn_fields_boots(settings):
    """The boot-gate question, asked where a CdnImageField actually exists.

    ``stapel_cdn`` is not on ``BOOT_GATE_TAGS`` (it walks models), so E002
    never refused a worker — it blocked `manage.py check`, `migrate` and
    `stapel_preflight` instead. Both halves are pinned here: the tag stays off
    the roster, and the roster is silent for this deployment.
    """
    from stapel_core.django.boot import BOOT_GATE_TAGS, run_boot_gates

    assert "stapel_cdn" not in BOOT_GATE_TAGS

    settings.CORS_ALLOW_ALL_ORIGINS = False
    settings.STAPEL_COMM = {"FUNCTION_TRANSPORT": "nats"}
    assert run_boot_gates() == []
