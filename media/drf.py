"""DRF surface for the source-agnostic image descriptor (images-and-cdn.md §5).

`stapel_core.media.image(source, value)` builds a `StapelImage` — the single
contract a frontend `<Image>` (`@stapel/image`) consumes for ANY image (CDN
ladder, plain file, or external OAuth link). This module is its serializer, so
any DRF serializer can denormalize an image NEXT TO the ref it already emits
and drf-spectacular emits a stable `StapelImage` component into that module's
schema.json.

THE DESIGN RULE: a media ref serialized for rendering must travel as a
`StapelImage`, never a bare `"<type>/<hash>"` string — a bare string leaves the
client unable to render anything but a guess (the 16px preview on a hero image,
or a full-res original in a 40px avatar).

Consume it with a `SerializerMethodField` returning `media.image(source, ref)`,
annotated so the shared component is emitted::

    from drf_spectacular.utils import extend_schema_field
    from stapel_core.media import image
    from stapel_core.media.drf import StapelImageSerializer

    avatar = serializers.SerializerMethodField()

    @extend_schema_field(StapelImageSerializer)
    def get_avatar(self, obj):
        return image(obj.avatar_source, obj.avatar)

The TypeScript side is the hand-mirror in `@stapel/image` (see `types.py`).
"""
from __future__ import annotations

from typing import Optional

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .providers import describe

__all__ = [
    "VariantMetaSerializer",
    "RenderMetadataSerializer",
    "StapelImageSerializer",
    "StapelImageArraySerializer",
    "describe_or_none",
]


@extend_schema_field({"type": "string"})
class _TierField(serializers.Field):
    """A ladder tier ON THE WIRE: always a string — a numeric px value as its
    decimal string (``"320"``), or the literal ``"original"``.

    Stringified here so the runtime output matches the DTO-declared schema (a
    dataclass ``Union[int, str]`` collapses to ``string`` under
    rest_framework_dataclasses), and every consumer sees ONE scalar type. The
    `@stapel/image` hand-mirror parses the numeric ones back for its tier math.
    """

    def to_representation(self, value):  # noqa: D102
        return str(value)

    def to_internal_value(self, data):  # noqa: D102
        return data


class VariantMetaSerializer(serializers.Serializer):
    """One generated variant file (or the original) — mirrors ``VariantMeta``."""

    tier = _TierField()
    branch = serializers.CharField(allow_null=True)
    url = serializers.CharField()
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)


class RenderMetadataSerializer(serializers.Serializer):
    """The CDN/PIL ladder snapshot — mirrors ``RenderMetadata`` (types.py).

    Kept for a bare-ref `describe` surface; ref-carrying responses prefer
    `StapelImageSerializer`, which wraps this with a ``source`` + top-level
    ``url`` so it renders even without a ladder.
    """

    mime = serializers.CharField()
    bytes = serializers.IntegerField()
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)
    aspect = serializers.FloatField(allow_null=True)
    duration_ms = serializers.IntegerField(allow_null=True)
    preview_b64 = serializers.CharField(allow_null=True)
    square = serializers.BooleanField()
    variants = VariantMetaSerializer(many=True)


class StapelImageSerializer(serializers.Serializer):
    """The source-agnostic image descriptor — mirrors ``StapelImage``.

    ``variants`` is the CDN/PIL ladder, or ``[]`` for a ``"link"`` / unprocessed
    file, in which case the renderer degrades to the single top-level ``url``.
    """

    source = serializers.CharField()
    url = serializers.CharField()
    mime = serializers.CharField(allow_null=True)
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)
    aspect = serializers.FloatField(allow_null=True)
    square = serializers.BooleanField()
    preview_b64 = serializers.CharField(allow_null=True)
    variants = VariantMetaSerializer(many=True)


class StapelImageArraySerializer(serializers.Serializer):
    """An ordered image gallery — mirrors ``StapelImageArray`` (catalog/listing
    photos). Profiles' avatar is a single `StapelImageSerializer`, not this."""

    images = StapelImageSerializer(many=True)
    primary = serializers.IntegerField()


def describe_or_none(ref: Optional[str]) -> Optional[dict]:
    """`describe(ref)` (the ladder-only snapshot) for a renderable ref, or
    ``None`` when there is nothing to render / the ref does not resolve.

    For a source-tagged image prefer `stapel_core.media.image(source, ref)`,
    which degrades to a single URL for link/file; this stays for a bare-ref
    describe surface (chat attachment lookups, etc.).
    """
    if not ref:
        return None
    try:
        return describe(ref)
    except (LookupError, ValueError):
        return None
