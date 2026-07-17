"""
Custom Django model fields for CDN image references.

These fields store CDN references in format: <type>/<id>
- ``ref_kind="hash"`` (default): <type>/<64-hex-hash> — the majority case
  for user-uploaded content (avatars, product photos, ...).
- ``ref_kind="slug"``: <type>/<free-form-name> — named assets such as
  catalog icons or carousel slides, where the "id" is a human-chosen name
  rather than a content hash.

``image_type`` is an open string, not a hardcoded enum (cdn-modularity.md
§2.1) — any host project can declare its own types via
``STAPEL_CDN["ASSET_TYPES"]`` (default ``("avatar",)``). This module only
validates the *shape* of ``image_type`` at field-construction time (a
cheap, config-independent slug check); whether the type is actually
*configured* for this deployment — and whether a cdn module/route is wired
at all — is checked lazily by ``stapel_core.django.cdn.checks`` (system
checks, tag ``stapel_cdn``), not at model-import time. Freezing this to a
fixed tuple and raising ``ValueError`` from ``__init__`` (the previous
behavior, inherited verbatim from the legacy marketplace ``ImageType``/
``AssetType`` enum) is exactly the "design that shouldn't allow this"
mistake cdn-modularity.md calls out — it made it impossible for a host
project to add a new CDN type without forking stapel-core.

Example usage:
    class MyModel(models.Model):
        # Hash-backed image field (default ref_kind)
        photo = CdnImageField(image_type='product')

        # Slug-backed named asset
        icon = CdnImageField(image_type='catalog', ref_kind='slug')

        # List of images (max 9 by default)
        gallery = CdnImageListField(image_type='product', max_images=5)
"""
import re
from typing import Any
from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from django import forms


# Valid ref_kind values — which regex validates the "<id>" half of a ref.
REF_KINDS = ('hash', 'slug')

# image_type must look like a slug: lowercase letters/digits/underscore/
# hyphen. Whether it's actually *configured* for this deployment is a
# separate, lazy question (system check, not this pattern).
IMAGE_TYPE_PATTERN = re.compile(r'^[a-z0-9_-]+$')

# Validation patterns for the "<id>" half of a ref, selected by ref_kind.
ASSET_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
IMAGE_HASH_PATTERN = re.compile(r'^[a-fA-F0-9]{64}$')


def validate_cdn_reference(value, image_type, ref_kind='hash'):
    """
    Validate a CDN reference value.

    Args:
        value: The reference string in format <type>/<id>
        image_type: Expected type (an open string, e.g. 'avatar', 'product')
        ref_kind: 'hash' (64-hex content hash) or 'slug' (free-form name) —
            selects which pattern the "<id>" half must match.

    Raises:
        ValidationError: If value doesn't match expected format
    """
    if not value:
        return  # Empty is OK (nullable field)

    if not isinstance(value, str):
        raise ValidationError(f"CDN reference must be a string, got {type(value).__name__}")

    # Parse type/id
    if '/' not in value:
        raise ValidationError(f"CDN reference must be in format 'type/id', got: {value}")

    ref_type, ref_id = value.split('/', 1)

    # Validate type matches expected
    if ref_type != image_type:
        raise ValidationError(f"CDN reference type mismatch: expected '{image_type}', got '{ref_type}'")

    # Validate ID format based on ref_kind
    if ref_kind == 'slug':
        if not ASSET_NAME_PATTERN.match(ref_id):
            raise ValidationError(
                f"Asset name can only contain letters, numbers, underscores and hyphens: {ref_id}"
            )
    else:
        if not IMAGE_HASH_PATTERN.match(ref_id):
            raise ValidationError(
                f"Image hash must be a 64-character hex string: {ref_id}"
            )


def _validate_image_type_shape(image_type: str) -> None:
    """Cheap, config-independent shape check — not a membership check.

    Raises ValueError for malformed identifiers (spaces, uppercase, empty).
    Whether ``image_type`` is *configured* for this deployment is checked
    lazily by the ``stapel_cdn`` system checks, never here.
    """
    if not isinstance(image_type, str) or not IMAGE_TYPE_PATTERN.match(image_type):
        raise ValueError(
            "image_type must be a lowercase slug matching "
            f"^[a-z0-9_-]+$, got: {image_type!r}"
        )


def _validate_ref_kind(ref_kind: str) -> None:
    if ref_kind not in REF_KINDS:
        raise ValueError(f"ref_kind must be one of {REF_KINDS}, got: {ref_kind!r}")


# ============================================================================
# Widgets (must be defined before fields that use them)
# ============================================================================

class CdnImageWidget(forms.TextInput):
    """Custom widget for CdnImageField that adds required data attributes."""

    class Media:
        js = ('admin/js/cdn_image_widget.js',)

    def __init__(self, image_type, ref_kind='hash', *args, **kwargs):
        self.image_type = image_type
        self.ref_kind = ref_kind
        super().__init__(*args, **kwargs)

    def get_context(self, name, value, attrs):
        attrs = attrs or {}
        attrs['data-cdn-image-type'] = self.image_type
        attrs['data-cdn-is-asset'] = 'true' if self.ref_kind == 'slug' else 'false'
        existing_class = attrs.get('class', '').strip()
        attrs['class'] = f'{existing_class} cdn-image-field'.strip()
        return super().get_context(name, value, attrs)


class CdnImageListWidget(forms.Textarea):
    """Custom widget for CdnImageListField that adds required data attributes."""

    class Media:
        js = ('admin/js/cdn_image_widget.js',)

    def __init__(self, image_type, max_images=9, ref_kind='hash', *args, **kwargs):
        self.image_type = image_type
        self.max_images = max_images
        self.ref_kind = ref_kind
        super().__init__(*args, **kwargs)

    def get_context(self, name, value, attrs):
        attrs = attrs or {}
        attrs['data-cdn-image-type'] = self.image_type
        attrs['data-cdn-is-asset'] = 'true' if self.ref_kind == 'slug' else 'false'
        attrs['data-cdn-max-images'] = str(self.max_images)
        existing_class = attrs.get('class', '').strip()
        attrs['class'] = f'{existing_class} cdn-image-list-field'.strip()
        return super().get_context(name, value, attrs)


# ============================================================================
# Model Fields
# ============================================================================

class CdnImageField(models.CharField):
    """
    Django model field for storing a single CDN image reference.

    Stores values in format: <type>/<id>
    - ref_kind='slug' (named assets, e.g. catalog/carousel): catalog/my-icon-name
    - ref_kind='hash' (default, user content, e.g. product/chat/avatar): product/abc123...def456

    Args:
        image_type: An open slug string (e.g. 'avatar', 'product', 'banner').
            Must be configured in this deployment's ``STAPEL_CDN["ASSET_TYPES"]``
            (default ``("avatar",)``) — enforced lazily by the ``stapel_cdn``
            system checks, not at class-definition time.
        ref_kind: 'hash' (default) or 'slug' — see above.
        max_length: Max length of the stored string (default 150)

    Example:
        class Category(models.Model):
            icon = CdnImageField(image_type='catalog', ref_kind='slug', blank=True, null=True)
    """
    description = "CDN image reference field"

    def __init__(self, image_type: str, ref_kind: str = 'hash', **kwargs: Any) -> None:
        _validate_image_type_shape(image_type)
        _validate_ref_kind(ref_kind)
        self.image_type = image_type
        self.ref_kind = ref_kind
        kwargs.setdefault('max_length', 150)
        super().__init__(**kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs['image_type'] = self.image_type
        if self.ref_kind != 'hash':
            kwargs['ref_kind'] = self.ref_kind
        if kwargs.get('max_length') == 150:
            del kwargs['max_length']
        return name, path, args, kwargs

    def validate(self, value, model_instance):
        super().validate(value, model_instance)
        validate_cdn_reference(value, self.image_type, self.ref_kind)

    def formfield(self, **kwargs):
        defaults = {
            'form_class': CdnImageFormField,
            'image_type': self.image_type,
            'ref_kind': self.ref_kind,
            'widget': CdnImageWidget(image_type=self.image_type, ref_kind=self.ref_kind),
        }
        defaults.update(kwargs)
        return super().formfield(**defaults)

    def contribute_to_class(self, cls, name):
        super().contribute_to_class(cls, name)
        # Add helper method to get the CDN URL. Unified with stapel-cdn's
        # canonical template `{base}{type}/{hash}/{tier}{branch}.webp`
        # (images-and-cdn.md §0.1 — this used to be a second, drifted
        # template). Tier semantics (0.6.0): thumbnail tiers (<= 120) are
        # single min-side files; preview tiers are branched — `branch`
        # defaults to "w", pass "h" for the height branch. The old
        # signature get_<name>_url(variant='720') keeps working and now
        # resolves to the w-branch file that actually exists.
        def get_cdn_url(instance, variant='720', branch=None):
            value = getattr(instance, name)
            if not value:
                return None
            ref_type, ref_id = value.split('/', 1)
            base = getattr(settings, 'STAPEL_CDN_MEDIA_URL', '/cdn/media/')
            try:
                tier = int(variant)
            except (TypeError, ValueError):
                # non-numeric variant ("original") — no branch suffix
                return f"{base}{ref_type}/{ref_id}/{variant}.webp"
            suffix = '' if tier <= 120 else (branch or 'w')
            return f"{base}{ref_type}/{ref_id}/{tier}{suffix}.webp"

        setattr(
            cls,
            f'get_{name}_url',
            lambda self, v='720', branch=None: get_cdn_url(self, v, branch),
        )


class CdnImageListField(models.JSONField):
    """
    Django model field for storing a list of CDN image references.

    Stores values as JSON array: ["type/id1", "type/id2", ...]

    Args:
        image_type: An open slug string — see ``CdnImageField``.
        max_images: Maximum number of images allowed (default 9)
        ref_kind: 'hash' (default) or 'slug' — see ``CdnImageField``.

    Example:
        class Ad(models.Model):
            photos = CdnImageListField(image_type='product', max_images=190)
    """
    description = "CDN image list field"

    def __init__(self, image_type: str, max_images: int = 9, ref_kind: str = 'hash', **kwargs: Any) -> None:
        _validate_image_type_shape(image_type)
        _validate_ref_kind(ref_kind)
        self.image_type = image_type
        self.max_images = max_images
        self.ref_kind = ref_kind
        kwargs.setdefault('default', list)
        kwargs.setdefault('blank', True)
        super().__init__(**kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs['image_type'] = self.image_type
        kwargs['max_images'] = self.max_images
        if self.ref_kind != 'hash':
            kwargs['ref_kind'] = self.ref_kind
        # Remove defaults
        if kwargs.get('default') is list:
            del kwargs['default']
        if kwargs.get('blank') is True:
            del kwargs['blank']
        return name, path, args, kwargs

    def validate(self, value, model_instance):
        super().validate(value, model_instance)

        if value is None:
            return

        if not isinstance(value, list):
            raise ValidationError(f"CdnImageListField must be a list, got {type(value).__name__}")

        if len(value) > self.max_images:
            raise ValidationError(
                f"Maximum {self.max_images} images allowed, got {len(value)}"
            )

        for i, item in enumerate(value):
            try:
                validate_cdn_reference(item, self.image_type, self.ref_kind)
            except ValidationError as e:
                raise ValidationError(f"Item {i}: {e.message}")

    def formfield(self, **kwargs):
        defaults = {
            'form_class': CdnImageListFormField,
            'image_type': self.image_type,
            'max_images': self.max_images,
            'ref_kind': self.ref_kind,
            'widget': CdnImageListWidget(image_type=self.image_type, max_images=self.max_images, ref_kind=self.ref_kind),
        }
        defaults.update(kwargs)
        return super().formfield(**defaults)


# ============================================================================
# Form Fields
# ============================================================================

class CdnImageFormField(forms.CharField):
    """Form field for CdnImageField with admin widget integration."""

    def __init__(self, image_type, ref_kind='hash', *args, **kwargs):
        self.image_type = image_type
        self.ref_kind = ref_kind
        kwargs.setdefault('widget', CdnImageWidget(image_type=image_type, ref_kind=ref_kind))
        super().__init__(*args, **kwargs)

    def validate(self, value):
        super().validate(value)
        if value:
            validate_cdn_reference(value, self.image_type, self.ref_kind)


class CdnImageListFormField(forms.JSONField):
    """Form field for CdnImageListField with admin widget integration."""

    def __init__(self, image_type, max_images=9, ref_kind='hash', *args, **kwargs):
        self.image_type = image_type
        self.max_images = max_images
        self.ref_kind = ref_kind
        kwargs.setdefault('widget', CdnImageListWidget(image_type=image_type, max_images=max_images, ref_kind=ref_kind))
        super().__init__(*args, **kwargs)

    def validate(self, value):
        super().validate(value)

        if value is None:
            return

        if not isinstance(value, list):
            raise ValidationError(f"Must be a list, got {type(value).__name__}")

        if len(value) > self.max_images:
            raise ValidationError(f"Maximum {self.max_images} images allowed")

        for i, item in enumerate(value):
            try:
                validate_cdn_reference(item, self.image_type, self.ref_kind)
            except ValidationError as e:
                raise ValidationError(f"Item {i}: {e.message}")
