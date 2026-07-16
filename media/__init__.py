"""stapel_core.media — one media interface, two storage paths (§61).

Code that renders media (chat, catalog, reviews) always calls
``stapel_core.media.describe(ref)`` and receives the same immutable
render-metadata snapshot (images-and-cdn.md §5) — whether the pixels live
in a plain Django ``ImageField`` processed by Pillow (``BACKEND="pil"``,
the zero-infrastructure default) or in the stapel-cdn service
(``BACKEND="cdn"``, the recommended production opt-in). Switching is
configuration (``STAPEL_MEDIA``/``STAPEL_MEDIA_BACKEND``), never a code
branch in the caller.

The variant-ladder core (min-side thumbnails, w/h preview branches, square
dedup, no upscaling) lives in :mod:`stapel_core.media.variants` as reusable
plan math + a PIL engine; stapel-cdn runs its pyvips engine over the same
semantics.
"""
from .conf import (
    DEFAULT_PREVIEW_SIZES,
    DEFAULT_THUMBNAIL_SIZES,
    MediaAppSettings,
    media_settings,
)
from .providers import (
    CdnRenderMetadataProvider,
    PilRenderMetadataProvider,
    RenderMetadataProvider,
    describe,
    get_provider,
)
from .types import RenderMetadata, VariantMeta
from .variants import (
    PlannedVariant,
    generate_variants,
    is_square,
    plan_variants,
    scaled_size,
    variant_name,
)

__all__ = [
    # facade
    "describe",
    "get_provider",
    # providers
    "RenderMetadataProvider",
    "PilRenderMetadataProvider",
    "CdnRenderMetadataProvider",
    # types
    "RenderMetadata",
    "VariantMeta",
    # ladder core
    "PlannedVariant",
    "plan_variants",
    "scaled_size",
    "is_square",
    "variant_name",
    "generate_variants",
    # conf
    "media_settings",
    "MediaAppSettings",
    "DEFAULT_THUMBNAIL_SIZES",
    "DEFAULT_PREVIEW_SIZES",
]
