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


#: Where an image's pixels live — the per-VALUE source tag (independent of the
#: deployment's media BACKEND). ``"cdn"``/``"file"`` are processed by a stapel
#: media provider and carry a variant ladder; ``"link"`` is an external URL
#: (e.g. an OAuth avatar) passed through untouched, no ladder.
ImageSource = str  # "cdn" | "file" | "link"


class StapelImage(TypedDict):
    """A SOURCE-AGNOSTIC image descriptor — the single contract a renderer
    (`@stapel/image` ``<Image>``) consumes for ANY image, whether or not a CDN
    is wired.

    A superset of `RenderMetadata`: it adds the ``source`` tag and an
    always-present top-level ``url`` (the canonical display URL), so a client
    can render even when there is no variant ladder. ``variants`` is the CDN/
    PIL ladder when present, or ``[]`` for a ``"link"`` (external URL) or an
    unprocessed file — the renderer degrades to the single ``url`` + ``aspect``
    (layout) + ``preview_b64`` (blur-up, when available). THE POINT: an avatar
    saved as a plain file, pulled from OAuth, or served off the CDN all render
    through the same component with the same contract.
    """

    source: ImageSource
    #: Always present — the canonical URL to display when there is no ladder
    #: (and the ladder's own ``"original"`` URL when there is).
    url: str
    mime: Optional[str]
    width: Optional[int]
    height: Optional[int]
    aspect: Optional[float]
    #: ``True`` ⇒ w/h branches are identical (one stored) — the renderer's
    #: branch-selection needs this to serve either axis from a square image.
    square: bool
    preview_b64: Optional[str]
    #: The variant ladder (CDN/PIL) or ``[]`` for link / unprocessed file.
    variants: List[VariantMeta]


class StapelImageArray(TypedDict):
    """An ordered image gallery (catalog/listing photos). ``primary`` indexes
    the cover image (``0`` by default). Profiles' avatar is deliberately NOT
    this — a single `StapelImage` field, so ``/me`` never ships a gallery's
    worth of metadata to show one face (owner directive 2026-07-20)."""

    images: List[StapelImage]
    primary: int


__all__ = [
    "RenderMetadata",
    "VariantMeta",
    "StapelImage",
    "StapelImageArray",
    "ImageSource",
]
