"""`stapel_core.media.image(...)` — build a source-agnostic `StapelImage`.

One builder every ref-carrying serializer calls to denormalize an image NEXT
TO the value it already stores, so a frontend `<Image>` (`@stapel/image`) can
render it regardless of where the pixels live.

CRITICAL — routing is by the per-value ``source`` tag, NOT the deployment's
global ``STAPEL_MEDIA_BACKEND``:

- ``source="cdn"`` — resolved through the **CDN** provider (`cdn.describe`
  comm), which reads stapel-cdn's OWN flat ``<hash>/{tier}{branch}.webp``
  variant naming. This is the fix for the live gap meettoday hit (libgaps Н3):
  a deployment whose default backend is ``"pil"`` was describing cdn-uploaded
  avatars with the PIL provider, which looks for a DIFFERENT naming
  (``<stem>__{tier}{branch}.webp``) and therefore found ZERO variants — the
  whole generated ladder invisible to `<Image>`. Tagging the value ``"cdn"``
  routes it to the provider that knows cdn's naming.
- ``source="file"`` — resolved through the **PIL** provider over plain Django
  storage (the zero-infrastructure path).
- ``source="link"`` — an external URL (an OAuth avatar, say) passed through
  untouched: no ladder, no processing, just ``url``.

THE DESIGN RULE (owner directive 2026-07-20): an image serialized for
rendering must travel as a `StapelImage`, never a bare ref string.
"""
from __future__ import annotations

from typing import Optional

from .providers import CdnRenderMetadataProvider, PilRenderMetadataProvider
from .types import ImageSource, StapelImage, VariantMeta

__all__ = ["image", "from_render_metadata"]


def _describe_by_source(source: ImageSource, value: str) -> Optional[dict]:
    """Route to the provider that OWNS ``source``'s variant naming, ignoring
    the global backend. ``None`` when the ref does not resolve."""
    if source == "cdn":
        provider = CdnRenderMetadataProvider()
    elif source == "file":
        provider = PilRenderMetadataProvider()
    else:
        return None
    try:
        return provider.describe(value)
    except (LookupError, ValueError):
        return None


def _original_url(variants: list[VariantMeta]) -> str:
    """The canonical display URL from a ladder: the ``"original"`` variant,
    else the largest tiered file, else empty — always something."""
    for v in variants:
        if v.get("tier") == "original":
            return v["url"]
    if not variants:
        return ""
    return max(variants, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))["url"]


def from_render_metadata(source: ImageSource, rm: dict) -> StapelImage:
    """Wrap a `RenderMetadata` snapshot (from a provider) as a `StapelImage`,
    tagging its ``source`` and lifting a top-level display ``url``."""
    variants = list(rm.get("variants") or [])
    return StapelImage(
        source=source,
        url=_original_url(variants),
        mime=rm.get("mime"),
        width=rm.get("width"),
        height=rm.get("height"),
        aspect=rm.get("aspect"),
        square=bool(rm.get("square")),
        preview_b64=rm.get("preview_b64"),
        variants=variants,
    )


def image(
    source: ImageSource,
    value: Optional[str],
    *,
    aspect: Optional[float] = None,
) -> Optional[StapelImage]:
    """Build a `StapelImage` for a stored image ``value`` tagged ``source``.

    Returns ``None`` when there is nothing to render (empty value, or a
    ``cdn``/``file`` ref that no longer resolves) — the caller's placeholder
    case, never a raised error, so one dangling ref never 500s a whole payload.

    ``aspect`` is an optional caller-known aspect ratio for a ``"link"`` image
    (external URLs can't be decoded server-side); ignored for cdn/file, whose
    aspect comes from the provider.
    """
    if not value:
        return None

    if source == "link":
        return StapelImage(
            source="link",
            url=value,
            mime=None,
            width=None,
            height=None,
            aspect=aspect,
            square=(aspect == 1.0),
            preview_b64=None,
            variants=[],
        )

    rm = _describe_by_source(source, value)
    if rm is None:
        return None
    return from_render_metadata(source, rm)
