"""Settings for ``stapel_core.media``, resolved through ``AppSettings``.

Configure via a ``STAPEL_MEDIA`` dict in Django settings::

    STAPEL_MEDIA = {
        "BACKEND": "pil",            # or "cdn", or a dotted provider path
        "THUMBNAIL_SIZES": [16, 32, 64, 120],
        "PREVIEW_SIZES": [160, 240, 480, 560, 720, 1080],
    }

The backend also honors the flat ``STAPEL_MEDIA_BACKEND`` Django setting /
environment variable (the spelling used across the docs) — same value,
same semantics.

Default backend is ``"pil"`` — the zero-infrastructure path (plain Django
``ImageField`` + Pillow variants next to the original). ``"cdn"`` (the
recommended production path: stapel-cdn service, content-addressed storage,
background processing) is an explicit opt-in of the host project.
"""
from __future__ import annotations

from stapel_core.conf import AppSettings

#: Thumbnail tiers (images-and-cdn.md §2.1/§3.4): min-side resize, no
#: branches. 16 is the micro tier inlined as ``preview_b64``.
DEFAULT_THUMBNAIL_SIZES = (16, 32, 64, 120)

#: Preview tiers (§2.1/§3.2): two branches per tier ({T}w / {T}h), so any
#: slot's limiting axis is served without upscaling.
DEFAULT_PREVIEW_SIZES = (160, 240, 480, 560, 720, 1080)

DEFAULTS = {
    # "pil" | "cdn" | dotted path to a RenderMetadataProvider class.
    "BACKEND": "pil",
    "THUMBNAIL_SIZES": DEFAULT_THUMBNAIL_SIZES,
    "PREVIEW_SIZES": DEFAULT_PREVIEW_SIZES,
    "WEBP_QUALITY": 85,
    # Optional watermark engine for preview tiers: dotted path to (or
    # directly a) callable ``(PIL.Image) -> PIL.Image``. Off by default.
    "WATERMARK": "",
    # |width - height| <= epsilon counts as square (§3.3 dedup).
    "SQUARE_EPSILON": 1,
}

_UNSET = object()


class MediaAppSettings(AppSettings):
    """AppSettings that also honors the flat ``STAPEL_MEDIA_BACKEND`` name."""

    FLAT_ALIASES = {"BACKEND": "STAPEL_MEDIA_BACKEND"}

    def _connect_reload(self):
        try:
            from django.test.signals import setting_changed

            def _reload(*, setting, **kwargs):
                if (
                    setting == self.namespace
                    or setting in self.defaults
                    or setting in self.FLAT_ALIASES.values()
                ):
                    self.reload()

            setting_changed.connect(_reload, weak=False)
        except Exception:  # pragma: no cover — Django not ready
            pass

    def _raw(self, key):
        import os

        from django.conf import settings

        overrides = getattr(settings, self.namespace, None) or {}
        if key not in overrides:
            alias = self.FLAT_ALIASES.get(key)
            if alias is not None:
                value = getattr(settings, alias, _UNSET)
                if value is not _UNSET:
                    return value
                env = os.environ.get(alias)
                if env is not None:
                    return env
        return super()._raw(key)


media_settings = MediaAppSettings(
    "STAPEL_MEDIA",
    defaults=DEFAULTS,
    import_strings=("WATERMARK",),
    # BACKEND is too generic a name to trust as a bare env var; the
    # explicit STAPEL_MEDIA_BACKEND env alias above covers the env path.
    no_env=("BACKEND",),
)

__all__ = [
    "media_settings",
    "MediaAppSettings",
    "DEFAULTS",
    "DEFAULT_THUMBNAIL_SIZES",
    "DEFAULT_PREVIEW_SIZES",
]
