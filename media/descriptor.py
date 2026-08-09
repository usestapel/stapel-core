"""`stapel_core.media.image(...)` — build a source-agnostic `StapelImage`.

One builder every ref-carrying serializer calls to denormalize an image NEXT
TO the value it already stores, so a frontend `<Image>` (`@stapel/image`) can
render it regardless of where the pixels live.

CRITICAL — routing is by the per-value ``source`` tag, NOT the deployment's
global ``STAPEL_MEDIA_BACKEND``:

- ``source="cdn"`` — resolved through the **CDN** provider (`cdn.describe`
  comm), which reads stapel-cdn's OWN flat ``<hash>/{tier}{branch}.webp``
  variant naming. This is the fix for the live gap meettoday hit (libgaps H3):
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

import logging
from typing import Optional

from .providers import CdnRenderMetadataProvider, PilRenderMetadataProvider
from .types import ImageSource, StapelImage, VariantMeta

__all__ = ["image", "from_render_metadata"]

logger = logging.getLogger(__name__)


def _describe_by_source(source: ImageSource, value: str) -> Optional[dict]:
    """Route to the provider that OWNS ``source``'s variant naming, ignoring
    the global backend. ``None`` when the ref does not resolve.

    THE GUARD IS BY CLASS, NOT BY EXCEPTION NAME. It used to catch
    ``(LookupError, ValueError)`` — the two types the providers were known to
    raise — and that is not the promise :func:`image` makes. Live on the
    meettoday sandbox a profile carried a stapel-cdn ref (a DIRECTORY holding
    the variant ladder) mis-tagged ``file``, so the PIL provider opened it as
    a plain file and raised ``IsADirectoryError`` — an ``OSError``, outside
    the tuple, straight past the guard. A cosmetic avatar 500'd
    ``GET /profiles/api/v1/me`` in full: the frontend then read no
    ``display_name``, concluded the account was unnamed, blocked the meeting
    door with an "enter your name" dialog, whose PATCH re-serialized the same
    avatar and 500'd again. Two people locked out of the product by a dangling
    ref that this function exists to absorb.

    So: ANY failure to resolve a ref degrades to ``None`` — but never
    silently. Silent ``None`` is how "no result" becomes indistinguishable
    from "a result", which is this fleet's recurring root class; every
    degrade below carries the source, the ref and the traceback at WARNING so
    the broken row is findable by grep instead of by outage.
    """
    if source == "cdn":
        provider = CdnRenderMetadataProvider()
    elif source == "file":
        provider = PilRenderMetadataProvider()
    else:
        logger.warning(
            "media.image: no provider owns source %r (ref %r) — no image", source, value
        )
        return None
    try:
        return provider.describe(value)
    except (LookupError, ValueError):
        # The expected dangling-ref shape: the provider looked and did not
        # find. Still logged — a ref stored on a row that no longer resolves
        # is a data defect, not a normal state.
        logger.warning(
            "media.image: %s ref %r does not resolve — rendering no image",
            source,
            value,
            exc_info=True,
        )
        return None
    except Exception:
        # Storage/transport/decoder faults (IsADirectoryError and the rest of
        # OSError, a comm failure to the CDN service, a codec blowing up on a
        # truncated file). Louder, because unlike a dangling ref these are not
        # supposed to happen at all — but still degraded, because `image()`
        # promises one bad ref never takes down the payload around it.
        logger.exception(
            "media.image: failed to describe %s ref %r — degrading to no image",
            source,
            value,
        )
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
    ``cdn``/``file`` ref that does not resolve — missing, unreadable, or of a
    shape its provider cannot describe) — the caller's placeholder case,
    NEVER a raised error, so one dangling ref never 500s a whole payload.
    That sentence is a contract, not a hope: see `_describe_by_source`, which
    absorbs the whole class of resolution failures and logs each one.

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
