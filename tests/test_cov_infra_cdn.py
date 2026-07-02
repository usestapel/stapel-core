"""Coverage tests for django/cdn/fields.py and django/cdn/ref_sync.py."""
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.db import models
from django.test import override_settings

from stapel_core.django.cdn.fields import (
    CDN_ASSET_TYPES,
    CDN_IMAGE_TYPES,
    CdnImageField,
    CdnImageFormField,
    CdnImageListField,
    CdnImageListFormField,
    CdnImageListWidget,
    CdnImageWidget,
    validate_cdn_reference,
)
from stapel_core.django.cdn.ref_sync import (
    RefSyncResult,
    check_cdn_media_exists,
    get_ref_sync_topic,
    sync_cdn_refs,
)

HASH64 = "a" * 64


# ---------------------------------------------------------------------------
# validate_cdn_reference
# ---------------------------------------------------------------------------


def test_validate_empty_value_ok():
    assert validate_cdn_reference("", "catalog") is None
    assert validate_cdn_reference(None, "product") is None


def test_validate_non_string_raises():
    with pytest.raises(ValidationError, match="must be a string"):
        validate_cdn_reference(123, "catalog")


def test_validate_missing_slash_raises():
    with pytest.raises(ValidationError, match="format 'type/id'"):
        validate_cdn_reference("catalogfoo", "catalog")


def test_validate_type_mismatch_raises():
    with pytest.raises(ValidationError, match="type mismatch"):
        validate_cdn_reference("product/" + HASH64, "catalog")


def test_validate_asset_name_ok_and_bad():
    validate_cdn_reference("catalog/my-icon_2", "catalog")
    with pytest.raises(ValidationError, match="Asset name"):
        validate_cdn_reference("catalog/bad name!", "catalog")


def test_validate_image_hash_ok_and_bad():
    validate_cdn_reference(f"product/{HASH64}", "product")
    with pytest.raises(ValidationError, match="64-character hex"):
        validate_cdn_reference("product/short", "product")


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------


def test_cdn_image_widget_context_asset():
    widget = CdnImageWidget(image_type="catalog")
    ctx = widget.get_context("icon", "catalog/x", None)
    attrs = ctx["widget"]["attrs"]
    assert attrs["data-cdn-image-type"] == "catalog"
    assert attrs["data-cdn-is-asset"] == "true"
    assert attrs["class"] == "cdn-image-field"


def test_cdn_image_widget_context_image_type_merges_class():
    widget = CdnImageWidget(image_type="product")
    ctx = widget.get_context("photo", None, {"class": "existing"})
    attrs = ctx["widget"]["attrs"]
    assert attrs["data-cdn-is-asset"] == "false"
    assert attrs["class"] == "existing cdn-image-field"


def test_cdn_image_list_widget_context():
    widget = CdnImageListWidget(image_type="product", max_images=5)
    ctx = widget.get_context("photos", "[]", None)
    attrs = ctx["widget"]["attrs"]
    assert attrs["data-cdn-image-type"] == "product"
    assert attrs["data-cdn-is-asset"] == "false"
    assert attrs["data-cdn-max-images"] == "5"
    assert attrs["class"] == "cdn-image-list-field"


def test_cdn_image_list_widget_context_asset():
    widget = CdnImageListWidget(image_type="carousel")
    ctx = widget.get_context("slides", None, {"class": "x"})
    attrs = ctx["widget"]["attrs"]
    assert attrs["data-cdn-is-asset"] == "true"
    assert attrs["class"] == "x cdn-image-list-field"


# ---------------------------------------------------------------------------
# CdnImageField
# ---------------------------------------------------------------------------


class CdnCovThing(models.Model):
    icon = CdnImageField(image_type="catalog", blank=True, null=True)
    photo = CdnImageField(image_type="product", blank=True, null=True)
    photos = CdnImageListField(image_type="product", max_images=2, null=True)

    class Meta:
        app_label = "users"


def test_cdn_image_field_invalid_type_raises():
    with pytest.raises(ValueError, match="image_type must be one of"):
        CdnImageField(image_type="bogus")


def test_cdn_image_field_types_constants():
    assert "catalog" in CDN_ASSET_TYPES
    assert "product" in CDN_IMAGE_TYPES


def test_cdn_image_field_deconstruct_default_max_length():
    field = CdnImageField(image_type="catalog")
    name, path, args, kwargs = field.deconstruct()
    assert kwargs["image_type"] == "catalog"
    assert "max_length" not in kwargs


def test_cdn_image_field_deconstruct_custom_max_length():
    field = CdnImageField(image_type="catalog", max_length=99)
    _, _, _, kwargs = field.deconstruct()
    assert kwargs["max_length"] == 99


def test_cdn_image_field_validate():
    obj = CdnCovThing()
    field = CdnCovThing._meta.get_field("icon")
    field.validate("catalog/nice-icon", obj)  # ok
    with pytest.raises(ValidationError):
        field.validate("product/" + HASH64, obj)


def test_cdn_image_field_formfield():
    field = CdnCovThing._meta.get_field("icon")
    ff = field.formfield()
    assert isinstance(ff, CdnImageFormField)
    assert ff.image_type == "catalog"
    assert isinstance(ff.widget, CdnImageWidget)


def test_cdn_image_field_url_helper():
    obj = CdnCovThing(icon="catalog/my-icon", photo=f"product/{HASH64}")
    assert obj.get_icon_url() == "/cdn/media/catalog/my-icon/720.webp"
    assert obj.get_icon_url("1080") == "/cdn/media/catalog/my-icon/1080.webp"
    assert obj.get_photo_url() == f"/cdn/media/product/{HASH64}/720.webp"
    empty = CdnCovThing()
    assert empty.get_icon_url() is None


# ---------------------------------------------------------------------------
# CdnImageListField
# ---------------------------------------------------------------------------


def test_cdn_image_list_field_invalid_type_raises():
    with pytest.raises(ValueError, match="image_type must be one of"):
        CdnImageListField(image_type="nope")


def test_cdn_image_list_field_deconstruct_removes_defaults():
    field = CdnImageListField(image_type="product", max_images=9)
    _, _, _, kwargs = field.deconstruct()
    assert kwargs["image_type"] == "product"
    assert kwargs["max_images"] == 9
    assert "default" not in kwargs
    assert "blank" not in kwargs


def test_cdn_image_list_field_deconstruct_keeps_overrides():
    field = CdnImageListField(image_type="product", default=None, null=True)
    _, _, _, kwargs = field.deconstruct()
    assert "default" in kwargs
    assert kwargs["null"] is True


def test_cdn_image_list_field_validate():
    obj = CdnCovThing()
    field = CdnCovThing._meta.get_field("photos")
    field.validate([f"product/{HASH64}"], obj)  # ok
    field.validate(None, obj)  # null=True -> early return
    with pytest.raises(ValidationError, match="must be a list"):
        field.validate("not-a-list", obj)
    with pytest.raises(ValidationError, match="Maximum 2 images"):
        field.validate([f"product/{HASH64}"] * 3, obj)
    with pytest.raises(ValidationError, match="Item 0"):
        field.validate(["catalog/wrong-type"], obj)


def test_cdn_image_list_field_formfield():
    field = CdnCovThing._meta.get_field("photos")
    ff = field.formfield()
    assert isinstance(ff, CdnImageListFormField)
    assert ff.image_type == "product"
    assert ff.max_images == 2
    assert isinstance(ff.widget, CdnImageListWidget)


# ---------------------------------------------------------------------------
# form fields
# ---------------------------------------------------------------------------


def test_cdn_image_form_field_validate():
    ff = CdnImageFormField(image_type="catalog", required=False)
    ff.validate("catalog/ok")
    ff.validate("")  # empty skips CDN validation
    with pytest.raises(ValidationError):
        ff.validate("catalog/bad name!")


def test_cdn_image_list_form_field_validate():
    ff = CdnImageListFormField(image_type="product", max_images=2, required=False)
    ff.validate([f"product/{HASH64}"])
    ff.validate(None)
    with pytest.raises(ValidationError, match="Must be a list"):
        ff.validate({"a": 1})
    with pytest.raises(ValidationError, match="Maximum 2 images"):
        ff.validate([f"product/{HASH64}"] * 3)
    with pytest.raises(ValidationError, match="Item 1"):
        ff.validate([f"product/{HASH64}", "product/bad"])


# ---------------------------------------------------------------------------
# ref_sync
# ---------------------------------------------------------------------------


def test_get_ref_sync_topic_default_and_override():
    assert get_ref_sync_topic() == "stapel.cdn.ref-sync"
    with override_settings(STAPEL_TOPIC_CDN_REF_SYNC="acme.cdn.sync"):
        assert get_ref_sync_topic() == "acme.cdn.sync"


def test_sync_cdn_refs_no_change_is_noop():
    result = sync_cdn_refs("profiles", "ad", 1, ["product/a"], ["product/a"])
    assert result == RefSyncResult(ok=True, errors=[])


def test_sync_cdn_refs_publishes_event():
    from stapel_core.bus import get_bus

    result = sync_cdn_refs(
        "profiles", "ad", 42, ["product/old"], ["product/new1", "product/new2"]
    )
    assert result.ok is True
    event = get_bus().events[-1]
    assert event.event_type == "cdn.ref.sync"
    assert event.service == "profiles"
    assert event.key == "42"
    assert event.payload["entity_type"] == "ad"
    assert event.payload["entity_id"] == "42"
    assert set(event.payload["old_hashes"]) == {"product/old"}
    assert set(event.payload["new_hashes"]) == {"product/new1", "product/new2"}


def test_sync_cdn_refs_handles_none_ref_lists():
    from stapel_core.bus import get_bus

    result = sync_cdn_refs("profiles", "ad", "x1", None, ["product/n"])
    assert result.ok is True
    assert get_bus().events[-1].payload["old_hashes"] == []


def test_sync_cdn_refs_publish_failure_returns_not_ok(monkeypatch):
    import stapel_core.bus as bus_pkg

    def boom(topic, event):
        raise RuntimeError("broker down")

    monkeypatch.setattr(bus_pkg, "publish", boom)
    result = sync_cdn_refs("profiles", "ad", 1, [], ["product/n"])
    assert result.ok is False


# ---------------------------------------------------------------------------
# check_cdn_media_exists
# ---------------------------------------------------------------------------


def test_check_cdn_media_exists_no_slash_returns_false():
    assert check_cdn_media_exists("nohash") is False


def test_check_cdn_media_exists_true():
    with mock.patch("requests.get") as get:
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"exists": True}
        with override_settings(CDN_SERVICE_URL="http://cdn:9", SERVICE_API_KEY="k1"):
            assert check_cdn_media_exists(f"product/{HASH64}") is True
    args, kwargs = get.call_args
    assert args[0] == "http://cdn:9/cdn/api/file/exists/"
    assert kwargs["params"] == {"file_hash": HASH64}
    assert kwargs["headers"] == {"X-API-KEY": "k1"}


def test_check_cdn_media_exists_false():
    with mock.patch("requests.get") as get:
        get.return_value.status_code = 200
        get.return_value.json.return_value = {}
        assert check_cdn_media_exists("product/abc") is False
    # no SERVICE_API_KEY setting -> no headers
    assert get.call_args[1]["headers"] == {}


def test_check_cdn_media_exists_error_status_raises():
    import requests as requests_lib

    with mock.patch("requests.get") as get:
        get.return_value.status_code = 500
        with pytest.raises(requests_lib.RequestException, match="status 500"):
            check_cdn_media_exists("product/abc")
