"""Render-metadata providers — one interface, two storage paths (§1).

``RenderMetadataProvider`` is the protocol both backends implement:

- **PIL** (default, zero infrastructure): originals live in a plain Django
  ``ImageField``/storage; variants are ``<stem>__<tier><branch>.webp``
  siblings produced by :func:`stapel_core.media.variants.generate_variants`.
  ``ref`` = the storage name of the original (the ``FieldFile.name`` value).
- **CDN** (recommended, explicit opt-in): delegates to the stapel-cdn
  service's ``cdn.describe`` comm Function. ``ref`` = ``<type>/<hash>``.

Callers never branch on the backend — ``stapel_core.media.describe(ref)``
routes here by ``STAPEL_MEDIA["BACKEND"]`` / ``STAPEL_MEDIA_BACKEND``.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from typing import List, Optional, Protocol, runtime_checkable

from .conf import media_settings
from .types import RenderMetadata, VariantMeta
from .variants import is_square, plan_variants, variant_name

logger = logging.getLogger(__name__)

__all__ = [
    "RenderMetadataProvider",
    "PilRenderMetadataProvider",
    "CdnRenderMetadataProvider",
    "get_provider",
    "describe",
]


@runtime_checkable
class RenderMetadataProvider(Protocol):
    """The single seam every media backend implements (§1)."""

    def describe(self, ref: str) -> RenderMetadata:  # pragma: no cover — protocol
        ...


class PilRenderMetadataProvider:
    """Snapshot over ImageField-style storage (§1a).

    Geometry of the variants is recomputed from the same pure plan math the
    generator used (``plan_variants``) — deterministic, no need to decode
    variant files; only existence is checked against the storage.
    """

    def __init__(self, storage=None):
        if storage is None:
            from django.core.files.storage import default_storage

            storage = default_storage
        self.storage = storage

    def describe(self, ref: str) -> RenderMetadata:
        from PIL import Image as PILImage

        storage = self.storage
        # `exists()` is NOT a guard that the ref names a readable image — it
        # answers True for a DIRECTORY on FileSystemStorage. That is exactly
        # how the meettoday sandbox got `IsADirectoryError` out of this
        # method: a stapel-cdn ref (a directory holding the variant ladder)
        # mis-tagged `file` sailed past this check and blew up on `open()`.
        # So the guard is the OPEN, not the stat: anything that cannot be
        # opened and decoded here is an unknown ref, reported as the
        # LookupError this method's contract already names.
        if not storage.exists(ref):
            raise LookupError(f"media.describe: unknown media ref {ref!r}")

        try:
            with storage.open(ref, "rb") as fh:
                img = PILImage.open(fh)
                width, height = img.size
        except (OSError, ValueError) as exc:
            # OSError covers IsADirectoryError, PermissionError, a storage
            # backend's transport fault, and PIL's own UnidentifiedImageError
            # / truncated-file errors (all OSError subclasses).
            raise LookupError(
                f"media.describe: media ref {ref!r} is not a readable image "
                f"({type(exc).__name__}: {exc})"
            ) from exc

        variants: List[VariantMeta] = []
        preview_b64: Optional[str] = None
        for planned in plan_variants(width, height):
            vname = variant_name(ref, planned.tier, planned.branch)
            if not storage.exists(vname):
                continue
            variants.append(
                VariantMeta(
                    tier=planned.tier,
                    branch=planned.branch,
                    url=self._url(vname),
                    width=planned.width,
                    height=planned.height,
                )
            )
            if planned.tier == 16 and planned.branch is None:
                # A ladder rung that cannot be read costs the blur-up, not the
                # whole description — the original decoded fine, so there IS
                # an image to render.
                try:
                    with storage.open(vname, "rb") as fh:
                        preview_b64 = "data:image/webp;base64," + base64.b64encode(
                            fh.read()
                        ).decode("ascii")
                except OSError:
                    logger.warning(
                        "media.describe: preview rung %r unreadable for ref %r "
                        "— describing without a blur-up",
                        vname,
                        ref,
                        exc_info=True,
                    )

        variants.append(
            VariantMeta(
                tier="original",
                branch=None,
                url=self._url(ref),
                width=width,
                height=height,
            )
        )

        try:
            size_bytes = storage.size(ref)
        except OSError:
            logger.warning(
                "media.describe: storage.size failed for ref %r", ref, exc_info=True
            )
            size_bytes = 0

        mime, _ = mimetypes.guess_type(ref)
        return RenderMetadata(
            mime=mime or "application/octet-stream",
            bytes=size_bytes,
            width=width,
            height=height,
            aspect=(width / height) if height else None,
            duration_ms=None,
            preview_b64=preview_b64,
            square=is_square(width, height),
            variants=variants,
        )

    def _url(self, name: str) -> str:
        try:
            return self.storage.url(name)
        except Exception:
            return "/" + name.lstrip("/")


class CdnRenderMetadataProvider:
    """Delegates to the stapel-cdn service over comm (§1b)."""

    def describe(self, ref: str) -> RenderMetadata:
        from stapel_core.comm import call

        return call("cdn.describe", {"ref": ref})


def get_provider() -> RenderMetadataProvider:
    """Provider selected by ``STAPEL_MEDIA["BACKEND"]`` (§1).

    ``"pil"`` | ``"cdn"`` | dotted path to a provider class (escape hatch).
    """
    backend = media_settings.BACKEND
    if backend == "pil":
        return PilRenderMetadataProvider()
    if backend == "cdn":
        return CdnRenderMetadataProvider()
    from django.utils.module_loading import import_string

    provider_cls = import_string(backend)
    return provider_cls()


def describe(ref: str) -> RenderMetadata:
    """The single entry point presenters/serializers call (§1): resolves a
    media ref into the immutable render-metadata snapshot, regardless of
    which backend stores the pixels."""
    return get_provider().describe(ref)
