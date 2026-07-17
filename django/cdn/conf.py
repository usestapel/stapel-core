"""Settings for ``stapel_core.django.cdn``, resolved through ``AppSettings``.

Configure via a ``STAPEL_CDN`` dict in Django settings::

    STAPEL_CDN = {
        "ASSET_TYPES": ("avatar", "banner"),
    }

``ASSET_TYPES`` is the single source of truth for which ``image_type``
values ``CdnImageField``/``CdnImageListField`` are allowed to declare in
*this* deployment (cdn-modularity.md §2.1/§5) — a host project without any
CDN service wired at all simply never sets this key and gets the
zero-infrastructure default, ``("avatar",)``: the one type every project
plausibly has (a profile avatar), nothing marketplace-specific
(``product``/``chat``/``review``) baked in by default.

``stapel-cdn`` (the server package) reads the *same* ``STAPEL_CDN``
namespace for parity — a project adds a type once, in one dict, and both
the client-side field validation (here) and the server-side upload/serve
path agree on what's legal.
"""
from __future__ import annotations

from stapel_core.conf import AppSettings

#: Zero-infrastructure default — the one CDN type every project plausibly
#: has (a profile avatar). Marketplace-specific types (product/chat/review)
#: are not baked in; a host project adds them explicitly.
DEFAULT_ASSET_TYPES = ("avatar",)

DEFAULTS = {
    "ASSET_TYPES": DEFAULT_ASSET_TYPES,
}

cdn_settings = AppSettings(
    "STAPEL_CDN",
    defaults=DEFAULTS,
    no_env=("ASSET_TYPES",),
)

__all__ = [
    "cdn_settings",
    "DEFAULTS",
    "DEFAULT_ASSET_TYPES",
]
