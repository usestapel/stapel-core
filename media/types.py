"""Render-metadata snapshot types (images-and-cdn.md §5).

The single form every media backend produces and every consumer
denormalizes ONCE when resolving a ref — chat attachments, catalog cards,
review photos. Mirrored by hand in TypeScript (`@stapel/image`
``RenderMetadata`` / ``VariantMeta``), like the rest of the project's DTOs.
"""
from __future__ import annotations

from typing import List, Optional, TypedDict, Union


class VariantMeta(TypedDict):
    """One generated variant file (or the original)."""

    #: Ladder tier (px along the branch axis) or the literal ``"original"``.
    tier: Union[int, str]
    #: ``"w"`` / ``"h"`` — preview branch; ``None`` for thumbnail-class
    #: (min-side) tiers and the original.
    branch: Optional[str]
    url: str
    #: Actual pixel geometry of the file (after the no-upscale cap).
    width: Optional[int]
    height: Optional[int]


class RenderMetadata(TypedDict):
    """Immutable snapshot produced on ingest, resolved via ``describe(ref)``."""

    mime: str
    bytes: int
    width: Optional[int]
    height: Optional[int]
    #: width / height; ``None`` when geometry is unknown (audio, files).
    aspect: Optional[float]
    #: Video/audio duration; ``None`` for still images.
    duration_ms: Optional[int]
    #: ``data:image/webp;base64,...`` — 16px micro tier (blur-up placeholder).
    preview_b64: Optional[str]
    #: ``True`` ⇒ w/h branches are identical, only one is stored (§3.3).
    square: bool
    variants: List[VariantMeta]


__all__ = ["RenderMetadata", "VariantMeta"]
