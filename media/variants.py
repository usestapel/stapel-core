"""Reusable variant-ladder core (images-and-cdn.md §1а, §3).

The tier semantics extracted from ``stapel_cdn.services`` as a library of
functions, engine-agnostic where possible:

- **plan math** (pure, no I/O): which files a given original produces —
  ``(tier, branch, axis, width, height)`` for min-side thumbnails (§3.4),
  w/h preview branches (§3.2) and the square dedup (§3.3);
- **PIL engine**: ``generate_variants`` renders that plan next to a Django
  ``FieldFile``/storage as ``<stem>__<tier><branch>.webp`` siblings — the
  zero-infrastructure ``ImageField`` path (§1а). stapel-cdn keeps its own
  pyvips engine over the same semantics (performance path).

No upscaling anywhere: a tier whose target side exceeds the native side is
saved at native size under the tier name.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .conf import media_settings
from .types import VariantMeta

__all__ = [
    "PlannedVariant",
    "scaled_size",
    "is_square",
    "plan_variants",
    "variant_name",
    "generate_variants",
]


@dataclass(frozen=True)
class PlannedVariant:
    """One file the pipeline will produce for an original of known geometry."""

    tier: int
    branch: Optional[str]  # None = thumbnail-class (min-side)
    axis: str  # "w" | "h" | "min"
    width: int  # actual pixel geometry after the no-upscale cap
    height: int


def scaled_size(width: int, height: int, target: int, axis: str) -> Tuple[int, int]:
    """Aspect-preserving downscale of (width, height) along one axis (§3.2).

    ``axis``: ``"w"`` — width == target; ``"h"`` — height == target;
    ``"min"`` — min(width, height) == target. Never upscales: if the native
    side is already <= target, returns the input unchanged.
    """
    if axis == "w":
        native = width
    elif axis == "h":
        native = height
    elif axis == "min":
        native = min(width, height)
    else:
        raise ValueError(f"scaled_size: unknown axis {axis!r}")
    if native <= target:
        return width, height
    scale = target / native
    return max(1, round(width * scale)), max(1, round(height * scale))


def is_square(width: int, height: int, epsilon: Optional[int] = None) -> bool:
    """Square within the dedup epsilon (§3.3): |w - h| <= epsilon (1px
    default — JPEG decode parity rounding)."""
    if epsilon is None:
        epsilon = int(media_settings.SQUARE_EPSILON)
    return abs(width - height) <= epsilon


def plan_variants(
    width: int,
    height: int,
    thumbnail_sizes: Optional[Sequence[int]] = None,
    preview_sizes: Optional[Sequence[int]] = None,
    epsilon: Optional[int] = None,
) -> List[PlannedVariant]:
    """The full file plan for an original of (width, height).

    Thumbnail tiers → one min-side file each (``branch None``); preview
    tiers → w- and h-branch files (w only when square). Pure math — the
    engines (PIL here, pyvips in stapel-cdn) render exactly this plan.
    """
    if thumbnail_sizes is None:
        thumbnail_sizes = [int(s) for s in media_settings.THUMBNAIL_SIZES]
    if preview_sizes is None:
        preview_sizes = [int(s) for s in media_settings.PREVIEW_SIZES]

    plan: List[PlannedVariant] = []
    for tier in sorted(thumbnail_sizes, reverse=True):
        w, h = scaled_size(width, height, tier, "min")
        plan.append(PlannedVariant(tier=tier, branch=None, axis="min", width=w, height=h))

    branches: Iterable[str] = ("w",) if is_square(width, height, epsilon) else ("w", "h")
    for axis in branches:
        for tier in sorted(preview_sizes, reverse=True):
            w, h = scaled_size(width, height, tier, axis)
            plan.append(PlannedVariant(tier=tier, branch=axis, axis=axis, width=w, height=h))
    return plan


def variant_name(original_name: str, tier: int, branch: Optional[str]) -> str:
    """Storage name of a variant file next to its original (§1а):
    ``<stem>__<tier><branch>.webp`` — e.g. ``photos/cat__560w.webp``."""
    stem, _dot, _ext = original_name.rpartition(".")
    if not stem:  # no extension in the original name
        stem = original_name
    return f"{stem}__{tier}{branch or ''}.webp"


def generate_variants(field_file, watermark=None) -> List[VariantMeta]:
    """Render the ladder for a Django ``FieldFile`` with Pillow (§1а).

    Variants are written through the field's own storage as sibling files
    (``variant_name``). Returns the ``VariantMeta`` list (persisting it —
    e.g. into a JSONField, computing it in ``post_save`` or a Celery task —
    is the host's hook, not this library's pipeline).

    ``watermark``: optional ``(PIL.Image) -> PIL.Image`` applied to preview
    tiers only (thumbnails stay clean, same split as stapel-cdn); defaults
    to the ``STAPEL_MEDIA["WATERMARK"]`` engine.
    """
    from io import BytesIO

    from django.core.files.base import ContentFile
    from PIL import Image as PILImage

    if watermark is None:
        watermark = media_settings.WATERMARK or None

    storage = field_file.storage
    name = field_file.name
    quality = int(media_settings.WEBP_QUALITY)

    with field_file.open("rb") as fh:
        original = PILImage.open(fh)
        original.load()
    if original.mode not in ("RGB", "RGBA"):
        original = original.convert("RGB")

    entries: List[VariantMeta] = []
    for planned in plan_variants(original.width, original.height):
        img = original.resize((planned.width, planned.height), PILImage.LANCZOS)
        if watermark is not None and planned.branch is not None:
            img = watermark(img)
        buf = BytesIO()
        img.save(buf, format="WEBP", quality=quality)
        vname = variant_name(name, planned.tier, planned.branch)
        if storage.exists(vname):
            storage.delete(vname)
        saved = storage.save(vname, ContentFile(buf.getvalue()))
        entries.append(
            VariantMeta(
                tier=planned.tier,
                branch=planned.branch,
                url=_storage_url(storage, saved),
                width=planned.width,
                height=planned.height,
            )
        )
    return entries


def _storage_url(storage, name: str) -> str:
    try:
        return storage.url(name)
    except Exception:  # storage without URL access — fall back to the name
        return posixpath.join("/", name)
