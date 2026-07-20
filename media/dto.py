"""Dataclass mirrors of the media descriptor, for the DTO-driven contract path.

`stapel_core.media.types` carries the `TypedDict` form (the `image()` builder's
return + the DRF serializer in `drf.py`). Modules whose OpenAPI contract is
declared from dataclasses (rest_framework_dataclasses / `StapelDataclassSerializer`
— e.g. stapel-profiles' `ProfileResponse`) need the SAME shape as a dataclass so
drf-spectacular emits a nested `StapelImage` component. This module is that
mirror — field-for-field identical to `types.StapelImage` / `types.VariantMeta`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class VariantMetaDTO:
    """Mirror of `types.VariantMeta`. ``tier`` ON THE WIRE is always a string —
    a numeric px value as its decimal string (``"320"``) or the literal
    ``"original"`` — matching the runtime `_TierField` (`drf.py`). The
    `@stapel/image` hand-mirror parses the numeric ones for its tier math."""

    tier: str
    branch: Optional[str]
    url: str
    width: Optional[int]
    height: Optional[int]


@dataclass
class StapelImageDTO:
    """Mirror of `types.StapelImage` — the source-agnostic image descriptor."""

    source: str
    url: str
    mime: Optional[str]
    width: Optional[int]
    height: Optional[int]
    aspect: Optional[float]
    square: bool
    preview_b64: Optional[str]
    variants: List[VariantMetaDTO] = field(default_factory=list)


def to_dto(img: Optional[dict]) -> Optional[StapelImageDTO]:
    """Convert an `image()`/`StapelImage` dict into its dataclass mirror
    (``tier`` stringified), or pass ``None`` through. For DTO-contract callers
    that build a `ProfileResponse`-style dataclass at runtime."""
    if img is None:
        return None
    return StapelImageDTO(
        source=img["source"],
        url=img["url"],
        mime=img.get("mime"),
        width=img.get("width"),
        height=img.get("height"),
        aspect=img.get("aspect"),
        square=bool(img.get("square")),
        preview_b64=img.get("preview_b64"),
        variants=[
            VariantMetaDTO(
                tier=str(v["tier"]),
                branch=v.get("branch"),
                url=v["url"],
                width=v.get("width"),
                height=v.get("height"),
            )
            for v in (img.get("variants") or [])
        ],
    )


__all__ = ["StapelImageDTO", "VariantMetaDTO", "to_dto"]
