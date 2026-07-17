"""Tests for stapel_core.django.cdn.conf and .checks (tag ``stapel_cdn``).

Covers the two owner-visible failure modes from cdn-modularity.md:

1. A declared ``CdnImageField``'s ``image_type`` is missing from this
   deployment's ``STAPEL_CDN["ASSET_TYPES"]`` (E001).
2. Any ``CdnImageField``/``CdnImageListField`` is declared, but no
   ``cdn.*`` comm route is configured at all — the miттudei incident (E002).
"""
from __future__ import annotations

import pytest
from django.db import models

from stapel_core.django.cdn.checks import (
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
    # No STAPEL_COMM route configured for cdn.* anywhere in the test settings
    # (tests/conftest.py doesn't wire one) — this is the miттudei scenario.
    errors = check_cdn_module_wired()
    assert len(errors) == 1
    assert errors[0].id == E002_CDN_ROUTE_MISSING
    assert "CdnChecksThing" in errors[0].msg


def test_e002_clean_when_route_configured(settings):
    settings.STAPEL_COMM = {
        "FUNCTION_ROUTES": {"cdn.": "http://stapel-cdn:8000/cdn"}
    }
    errors = check_cdn_module_wired()
    assert errors == []
