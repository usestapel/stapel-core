"""
Custom Django model fields for CDN image references.

These fields store CDN references in format: <type>/<id>
- Asset types (catalog, carousel): <type>/<asset_name>
- Image types (product, chat, avatar): <type>/<hash>

Example usage:
    class MyModel(models.Model):
        # Single image field
        icon = CdnImageField(image_type='catalog')
        photo = CdnImageField(image_type='product')

        # List of images (max 9 by default)
        gallery = CdnImageListField(image_type='product', max_images=5)
"""
import re
from typing import Any
from django.db import models
from django.core.exceptions import ValidationError
from django import forms


# Valid CDN image types
CDN_ASSET_TYPES = ('catalog', 'carousel')
CDN_IMAGE_TYPES = ('product', 'chat', 'avatar', 'review')
CDN_ALL_TYPES = CDN_ASSET_TYPES + CDN_IMAGE_TYPES

# Validation patterns
ASSET_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')
IMAGE_HASH_PATTERN = re.compile(r'^[a-fA-F0-9]{64}$')


def validate_cdn_reference(value, image_type):
    """
    Validate a CDN reference value.

    Args:
        value: The reference string in format <type>/<id>
        image_type: Expected type (catalog, carousel, product, chat, avatar)

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

    # Validate ID format based on type
    if image_type in CDN_ASSET_TYPES:
        if not ASSET_NAME_PATTERN.match(ref_id):
            raise ValidationError(
                f"Asset name can only contain letters, numbers, underscores and hyphens: {ref_id}"
            )
    else:
        if not IMAGE_HASH_PATTERN.match(ref_id):
            raise ValidationError(
                f"Image hash must be a 64-character hex string: {ref_id}"
            )


# ============================================================================
# Widgets (must be defined before fields that use them)
# ============================================================================

class CdnImageWidget(forms.TextInput):
    """Custom widget for CdnImageField that adds required data attributes."""

    class Media:
        js = ('admin/js/cdn_image_widget.js',)

    def __init__(self, image_type, *args, **kwargs):
        self.image_type = image_type
        super().__init__(*args, **kwargs)

    def get_context(self, name, value, attrs):
        attrs = attrs or {}
        attrs['data-cdn-image-type'] = self.image_type
        attrs['data-cdn-is-asset'] = 'true' if self.image_type in CDN_ASSET_TYPES else 'false'
        existing_class = attrs.get('class', '').strip()
        attrs['class'] = f'{existing_class} cdn-image-field'.strip()
        return super().get_context(name, value, attrs)


class CdnImageListWidget(forms.Textarea):
    """Custom widget for CdnImageListField that adds required data attributes."""

    class Media:
        js = ('admin/js/cdn_image_widget.js',)

    def __init__(self, image_type, max_images=9, *args, **kwargs):
        self.image_type = image_type
        self.max_images = max_images
        super().__init__(*args, **kwargs)

    def get_context(self, name, value, attrs):
        attrs = attrs or {}
        attrs['data-cdn-image-type'] = self.image_type
        attrs['data-cdn-is-asset'] = 'true' if self.image_type in CDN_ASSET_TYPES else 'false'
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
    - For assets (catalog/carousel): catalog/my-icon-name
    - For images (product/chat/avatar): product/abc123...def456

    Args:
        image_type: One of 'catalog', 'carousel', 'product', 'chat', 'avatar'
        max_length: Max length of the stored string (default 150)

    Example:
        class Category(models.Model):
            icon = CdnImageField(image_type='catalog', blank=True, null=True)
    """
    description = "CDN image reference field"

    def __init__(self, image_type: str, **kwargs: Any) -> None:
        if image_type not in CDN_ALL_TYPES:
            raise ValueError(
                f"image_type must be one of {CDN_ALL_TYPES}, got: {image_type}"
            )
        self.image_type = image_type
        kwargs.setdefault('max_length', 150)
        super().__init__(**kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs['image_type'] = self.image_type
        if kwargs.get('max_length') == 150:
            del kwargs['max_length']
        return name, path, args, kwargs

    def validate(self, value, model_instance):
        super().validate(value, model_instance)
        validate_cdn_reference(value, self.image_type)

    def formfield(self, **kwargs):
        defaults = {
            'form_class': CdnImageFormField,
            'image_type': self.image_type,
            'widget': CdnImageWidget(image_type=self.image_type),
        }
        defaults.update(kwargs)
        return super().formfield(**defaults)

    def contribute_to_class(self, cls, name):
        super().contribute_to_class(cls, name)
        # Add helper method to get the CDN URL
        def get_cdn_url(instance, variant='720'):
            value = getattr(instance, name)
            if not value:
                return None
            ref_type, ref_id = value.split('/', 1)
            # Build CDN URL based on type
            return f"/cdn/media/{ref_type}/{ref_id}/{variant}.webp"

        setattr(cls, f'get_{name}_url', lambda self, v='720': get_cdn_url(self, v))


class CdnImageListField(models.JSONField):
    """
    Django model field for storing a list of CDN image references.

    Stores values as JSON array: ["type/id1", "type/id2", ...]

    Args:
        image_type: One of 'catalog', 'carousel', 'product', 'chat', 'avatar'
        max_images: Maximum number of images allowed (default 9)

    Example:
        class Ad(models.Model):
            photos = CdnImageListField(image_type='product', max_images=190)
    """
    description = "CDN image list field"

    def __init__(self, image_type: str, max_images: int = 9, **kwargs: Any) -> None:
        if image_type not in CDN_ALL_TYPES:
            raise ValueError(
                f"image_type must be one of {CDN_ALL_TYPES}, got: {image_type}"
            )
        self.image_type = image_type
        self.max_images = max_images
        kwargs.setdefault('default', list)
        kwargs.setdefault('blank', True)
        super().__init__(**kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs['image_type'] = self.image_type
        kwargs['max_images'] = self.max_images
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
                validate_cdn_reference(item, self.image_type)
            except ValidationError as e:
                raise ValidationError(f"Item {i}: {e.message}")

    def formfield(self, **kwargs):
        defaults = {
            'form_class': CdnImageListFormField,
            'image_type': self.image_type,
            'max_images': self.max_images,
            'widget': CdnImageListWidget(image_type=self.image_type, max_images=self.max_images),
        }
        defaults.update(kwargs)
        return super().formfield(**defaults)


# ============================================================================
# Form Fields
# ============================================================================

class CdnImageFormField(forms.CharField):
    """Form field for CdnImageField with admin widget integration."""

    def __init__(self, image_type, *args, **kwargs):
        self.image_type = image_type
        kwargs.setdefault('widget', CdnImageWidget(image_type=image_type))
        super().__init__(*args, **kwargs)

    def validate(self, value):
        super().validate(value)
        if value:
            validate_cdn_reference(value, self.image_type)


class CdnImageListFormField(forms.JSONField):
    """Form field for CdnImageListField with admin widget integration."""

    def __init__(self, image_type, max_images=9, *args, **kwargs):
        self.image_type = image_type
        self.max_images = max_images
        kwargs.setdefault('widget', CdnImageListWidget(image_type=image_type, max_images=max_images))
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
                validate_cdn_reference(item, self.image_type)
            except ValidationError as e:
                raise ValidationError(f"Item {i}: {e.message}")
