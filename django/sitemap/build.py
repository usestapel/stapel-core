"""Provider registry, aggregation, rendering and caching of a site's sitemaps.

The provider contract (comm Function, any module, reached by name only)::

    request  {"site": str, "locale": str | null, "cursor": str | null, "limit": int}
    response {"entries": [Entry], "cursor": str | null}

    Entry = {"path": "/about",              # site-relative, starts with "/"
             # or "loc": "https://<site host>/about" (absolute, same site)
             "lastmod": "2026-11-12T10:00:00+00:00",   # optional, ISO 8601
             "locale": "en",                           # optional
             "alternates": [{"locale": "de", "path": "/de/about"}]}  # optional

``site`` is the registry site's key (:attr:`stapel_core.sites.Site.key`,
``""`` on a single-host deployment), ``locale`` is null (every locale), a
provider may ignore ``limit``, and a null ``cursor`` in the answer means the
provider is done. ``next_cursor`` is accepted as a synonym of ``cursor``.
Unknown entry keys are ignored.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, quoteattr

from django.utils.encoding import iri_to_uri

from .conf import sitemap_settings

logger = logging.getLogger(__name__)

__all__ = [
    "SitemapSet",
    "build_sitemaps",
    "collect_entries",
    "get_page",
    "get_sitemaps",
    "invalidate_sitemaps",
    "register_sitemap_provider",
    "render_index",
    "render_pages",
    "sitemap_providers",
    "unregister_sitemap_provider",
]

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
XHTML_NS = "http://www.w3.org/1999/xhtml"
CACHE_VERSION = "v1"

_lock = threading.Lock()
_registered: dict[str, str] = {}


def register_sitemap_provider(function: str, *, name: str | None = None) -> None:
    """Register the comm Function *function* as a sitemap provider.

    *name* defaults to the Function's module prefix (``"cms"`` for
    ``"cms.sitemap_entries"``). ``STAPEL_SITEMAP["PROVIDERS"]`` is merged over
    these registrations: it may add, replace or (with ``None``) remove one.
    """
    function = str(function).strip()
    if not function:
        raise ValueError("register_sitemap_provider: empty Function name")
    key = name or function.split(".", 1)[0]
    with _lock:
        _registered[key] = function


def unregister_sitemap_provider(name: str) -> None:
    with _lock:
        _registered.pop(name, None)


def sitemap_providers() -> dict[str, str]:
    """``{name: function}`` — code registrations with the settings merged over."""
    with _lock:
        merged = dict(_registered)
    for key, function in dict(sitemap_settings.PROVIDERS or {}).items():
        if function is None:
            merged.pop(key, None)
        else:
            merged[key] = str(function)
    return dict(sorted(merged.items()))


@dataclass
class Url:
    loc: str
    lastmod: datetime | None = None
    alternates: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Collected:
    urls: list[Url]
    skipped: list[str]
    truncated: bool = False


def _parse_lastmod(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _site_netloc(origin: str) -> str:
    return urlsplit(origin).netloc.lower()


def _to_loc(item, origin: str, default_locale: str) -> str | None:
    """A provider's ``path``/``loc`` -> an absolute URL on this site, or None."""
    if not isinstance(item, dict):
        return None
    loc = item.get("loc")
    if isinstance(loc, str) and loc:
        parts = urlsplit(loc)
        if parts.scheme not in ("http", "https") or parts.netloc.lower() != _site_netloc(origin):
            return None
        return iri_to_uri(loc)
    path = item.get("path")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return None
    locale = item.get("locale") if isinstance(item.get("locale"), str) else default_locale
    url = str(sitemap_settings.URL_TEMPLATE).format(origin=origin, path=path, locale=locale or "")
    return iri_to_uri(url)


def _entry_to_url(entry, origin: str) -> Url | None:
    loc = _to_loc(entry, origin, "")
    if loc is None:
        return None
    alternates = []
    for alt in entry.get("alternates") or []:
        if not isinstance(alt, dict) or not isinstance(alt.get("locale"), str):
            continue
        alt_loc = _to_loc(alt, origin, alt["locale"])
        if alt_loc is not None:
            alternates.append((alt["locale"], alt_loc))
    return Url(loc=loc, lastmod=_parse_lastmod(entry.get("lastmod")), alternates=alternates)


def collect_entries(site_key: str, origin: str) -> Collected:
    """Ask every registered provider for its entries of *site_key*.

    A provider that is not reachable, raises, times out or answers a
    malformed page is skipped for the rest of this build and logged once;
    entries it gave before failing are kept. Duplicate URLs are dropped.
    Stops at ``MAX_URLS`` and says so.
    """
    from stapel_core.comm import call

    batch = int(sitemap_settings.BATCH_LIMIT)
    max_urls = int(sitemap_settings.MAX_URLS)
    timeout = float(sitemap_settings.CALL_TIMEOUT)
    urls: list[Url] = []
    seen: set[str] = set()
    skipped: list[str] = []

    for name, function in sitemap_providers().items():
        cursor = None
        cursors_seen: set[str] = set()
        while True:
            payload = {"site": site_key, "locale": None, "cursor": cursor, "limit": batch}
            try:
                answer = call(function, payload, timeout=timeout)
            except Exception as exc:
                logger.warning(
                    "sitemap: provider %s (%s) did not answer for site %r; skipped: %s",
                    name, function, site_key, exc,
                )
                skipped.append(name)
                break
            entries = answer.get("entries") if isinstance(answer, dict) else None
            if not isinstance(entries, list):
                logger.warning(
                    "sitemap: provider %s (%s) answered without an entries list for site %r; skipped",
                    name, function, site_key,
                )
                skipped.append(name)
                break
            for entry in entries:
                url = _entry_to_url(entry, origin)
                if url is None or url.loc in seen:
                    continue
                if len(urls) >= max_urls:
                    logger.warning("sitemap: site %r truncated at %d URLs (MAX_URLS)", site_key, max_urls)
                    return Collected(urls=urls, skipped=skipped, truncated=True)
                seen.add(url.loc)
                urls.append(url)
            nxt = answer.get("cursor", answer.get("next_cursor"))
            if not nxt:
                break
            nxt = str(nxt)
            if nxt in cursors_seen:
                logger.warning(
                    "sitemap: provider %s (%s) repeated cursor %r for site %r; stopped",
                    name, function, nxt, site_key,
                )
                skipped.append(name)
                break
            cursors_seen.add(nxt)
            cursor = nxt
    return Collected(urls=urls, skipped=skipped)


def _w3c(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _url_xml(url: Url) -> str:
    parts = [f"<url><loc>{escape(url.loc)}</loc>"]
    if url.lastmod is not None:
        parts.append(f"<lastmod>{_w3c(url.lastmod)}</lastmod>")
    for hreflang, href in url.alternates:
        parts.append(
            f'<xhtml:link rel="alternate" hreflang={quoteattr(hreflang)} href={quoteattr(href)}/>'
        )
    parts.append("</url>\n")
    return "".join(parts)


_PAGE_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<urlset xmlns="{SITEMAP_NS}" xmlns:xhtml="{XHTML_NS}">\n'
)
_PAGE_TAIL = "</urlset>\n"


def render_pages(urls: list[Url]) -> list[tuple[str, datetime | None]]:
    """``[(xml, newest lastmod)]`` — pages within PAGE_MAX_URLS / PAGE_MAX_BYTES."""
    max_urls = int(sitemap_settings.PAGE_MAX_URLS)
    max_bytes = int(sitemap_settings.PAGE_MAX_BYTES)
    frame = len(_PAGE_HEAD.encode()) + len(_PAGE_TAIL.encode())
    pages: list[tuple[str, datetime | None]] = []
    body: list[str] = []
    size = frame
    newest: datetime | None = None

    def close():
        pages.append((_PAGE_HEAD + "".join(body) + _PAGE_TAIL, newest))

    for url in urls:
        chunk = _url_xml(url)
        chunk_size = len(chunk.encode())
        if frame + chunk_size > max_bytes:
            logger.warning("sitemap: %s alone exceeds PAGE_MAX_BYTES; left out", url.loc)
            continue
        if body and (len(body) >= max_urls or size + chunk_size > max_bytes):
            close()
            body, size, newest = [], frame, None
        body.append(chunk)
        size += chunk_size
        if url.lastmod is not None and (newest is None or url.lastmod > newest):
            newest = url.lastmod
    if body or not pages:
        close()
    return pages


def render_index(page_urls: list[str], lastmods: list[str | None]) -> str:
    rows = []
    for loc, lastmod in zip(page_urls, lastmods):
        row = f"<sitemap><loc>{escape(loc)}</loc>"
        if lastmod:
            row += f"<lastmod>{lastmod}</lastmod>"
        rows.append(row + "</sitemap>\n")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<sitemapindex xmlns="{SITEMAP_NS}">\n' + "".join(rows) + "</sitemapindex>\n"
    )


@dataclass
class SitemapSet:
    """A built set of one site: page ``i`` (1-based) is ``pages[i - 1]``.

    ``pages`` is only filled on the set :func:`build_sitemaps` returns; the
    cached copy carries the metadata and each page lives under its own key.
    """

    origin: str
    generation: str
    lastmods: list[str | None]
    complete: bool
    skipped: list[str]
    truncated: bool
    pages: list[str] | None = None

    @property
    def count(self) -> int:
        return len(self.lastmods)


def _cache():
    from django.core.cache import caches

    return caches[str(sitemap_settings.CACHE_ALIAS)]


def _meta_key(site_key: str) -> str:
    import hashlib

    digest = hashlib.sha256(site_key.encode()).hexdigest()[:24]
    return f"stapel:sitemap:{CACHE_VERSION}:{digest}"


def build_sitemaps(site_key: str, origin: str) -> SitemapSet:
    """Collect, render and cache the sitemap pages of one site (warms the cache)."""
    collected = collect_entries(site_key, origin)
    pages = render_pages(collected.urls)
    complete = not collected.skipped
    ttl = int(sitemap_settings.CACHE_TTL if complete else sitemap_settings.PARTIAL_CACHE_TTL)
    built = SitemapSet(
        origin=origin,
        generation=uuid.uuid4().hex,
        lastmods=[_w3c(newest) if newest else None for _, newest in pages],
        complete=complete,
        skipped=collected.skipped,
        truncated=collected.truncated,
        pages=[xml for xml, _ in pages],
    )
    if ttl > 0:
        meta_key = _meta_key(site_key)
        cache = _cache()
        cache.set_many(
            {f"{meta_key}:{built.generation}:{i}": xml for i, xml in enumerate(built.pages, 1)},
            timeout=ttl,
        )
        cache.set(meta_key, replace(built, pages=None), timeout=ttl)
    return built


def get_sitemaps(site_key: str, origin: str) -> SitemapSet:
    """The cached set of a site, built on a miss (or when its origin changed)."""
    if int(sitemap_settings.CACHE_TTL) > 0:
        hit = _cache().get(_meta_key(site_key))
        if isinstance(hit, SitemapSet) and hit.origin == origin:
            return hit
    return build_sitemaps(site_key, origin)


def get_page(site_key: str, built: SitemapSet, page: int) -> str | None:
    """XML of page *page* (1-based) of *built*, or None when out of range."""
    if page < 1 or page > built.count:
        return None
    if built.pages is not None:
        return built.pages[page - 1]
    xml = _cache().get(f"{_meta_key(site_key)}:{built.generation}:{page}")
    if xml is None:
        # The page expired before its index: rebuild once and serve that.
        return get_page(site_key, build_sitemaps(site_key, built.origin), page)
    return xml


def invalidate_sitemaps(site_key: str) -> None:
    """Drop a site's cached set (e.g. on a content publication fact)."""
    _cache().delete(_meta_key(site_key))
