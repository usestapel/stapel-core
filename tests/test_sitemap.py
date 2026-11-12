"""The site-scoped sitemap aggregator (stapel_core.django.sitemap).

Providers are fake comm Functions registered by name — the aggregator never
imports a module, so a fake behind the Function seam is exactly what a real
provider looks like to it.
"""
import logging
import os
import xml.etree.ElementTree as ET

import pytest
from django.core.cache import caches
from django.test import RequestFactory, override_settings

from stapel_core.comm.registry import function_registry
from stapel_core.django.sitemap import (
    SitemapIndexView,
    SitemapPageView,
    build_sitemaps,
    collect_entries,
    get_sitemap_urls,
    invalidate_sitemaps,
    register_sitemap_provider,
    render_pages,
    sitemap_providers,
    unregister_sitemap_provider,
)
from stapel_core.django.sitemap.build import Url
from stapel_core.sites import reset_sites_cache

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9", "xhtml": "http://www.w3.org/1999/xhtml"}
rf = RequestFactory()

ACME = {"host": "example.com", "aliases": ["www.example.com"], "primary": True, "locale": "en",
        "brand": {"key": "acme", "name": "Acme", "title": "Acme"}, "seo": {"index": True}}
NORD = {"host": "example.org", "primary": False, "locale": "de",
        "brand": {"key": "nord", "name": "Nord", "title": "Nord"}, "seo": {"index": False}}

CALLS: list[dict] = []
FAKES = ("fake_pages.sitemap_entries", "fake_listings.sitemap_entries", "fake_broken.sitemap_entries",
         "fake_garbage.sitemap_entries", "fake_loop.sitemap_entries")


def pages_provider(payload):
    """cms's shape: path, locale, kind, lastmod, alternates; keyset cursor."""
    CALLS.append(payload)
    rows = {
        "acme": [
            {"path": "/about", "locale": "en", "kind": "page", "lastmod": "2026-11-01T10:00:00+00:00",
             "alternates": [{"locale": "en", "path": "/about"}, {"locale": "de", "path": "/de/ueber"}]},
            {"path": "/terms", "locale": "en", "kind": "page", "lastmod": "2026-11-05T10:00:00Z"},
            {"path": "/blog/hello", "locale": "en", "kind": "post", "lastmod": "not a date"},
        ],
        "": [{"path": "/only", "lastmod": None}],
    }.get(payload["site"], [])
    start = int(payload.get("cursor") or 0)
    limit = int(payload.get("limit") or 2)
    chunk = rows[start:start + limit]
    nxt = start + limit
    return {"entries": chunk, "cursor": str(nxt) if nxt < len(rows) else None}


def listings_provider(payload):
    CALLS.append(payload)
    return {"entries": [{"loc": "https://example.com/l/1"},
                        {"loc": "https://evil.example/l/2"},
                        {"path": "/about"},
                        {"path": "//evil.example/x"},
                        {"path": "relative"}],
            "next_cursor": None}


def broken_provider(payload):
    raise RuntimeError("provider down")


def garbage_provider(payload):
    return {"rows": []}


def loop_provider(payload):
    return {"entries": [{"path": f"/loop/{payload.get('cursor') or 0}"}], "cursor": "same"}


@pytest.fixture(autouse=True)
def _env():
    saved = {n: os.environ.pop(n, None) for n in ("STAPEL_SITES_FILE", "STAPEL_SITES_JSON")}
    function_registry.register(FAKES[0], pages_provider)
    function_registry.register(FAKES[1], listings_provider)
    function_registry.register(FAKES[2], broken_provider)
    function_registry.register(FAKES[3], garbage_provider)
    function_registry.register(FAKES[4], loop_provider)
    CALLS.clear()
    reset_sites_cache()
    caches["default"].clear()
    yield
    for name in ("fake_pages", "fake_listings", "fake_broken", "fake_garbage", "fake_loop", "custom"):
        unregister_sitemap_provider(name)
    for name in FAKES:
        function_registry._providers.pop(name, None)
        function_registry._schemas.pop(name, None)
    caches["default"].clear()
    reset_sites_cache()
    for name, value in saved.items():
        if value is not None:
            os.environ[name] = value


def _locs(xml):
    root = ET.fromstring(xml)
    return [el.text for el in root.findall("sm:url/sm:loc", NS)]


class TestRegistry:
    def test_code_registration_named_by_module_prefix(self):
        register_sitemap_provider("fake_pages.sitemap_entries")
        assert sitemap_providers() == {"fake_pages": "fake_pages.sitemap_entries"}

    def test_settings_merge_over_code_and_none_removes(self):
        register_sitemap_provider("fake_pages.sitemap_entries")
        register_sitemap_provider("fake_listings.sitemap_entries")
        with override_settings(STAPEL_SITEMAP={"PROVIDERS": {
            "fake_listings": None, "custom": "fake_broken.sitemap_entries",
        }}):
            assert sitemap_providers() == {
                "custom": "fake_broken.sitemap_entries",
                "fake_pages": "fake_pages.sitemap_entries",
            }

    def test_empty_function_name_refused(self):
        with pytest.raises(ValueError):
            register_sitemap_provider("  ")


class TestCollect:
    @pytest.fixture(autouse=True)
    def _settings(self, settings):
        settings.STAPEL_SITES = {"sites": [ACME, NORD]}
        settings.STAPEL_SITEMAP = {"BATCH_LIMIT": 2}

    def test_pages_through_cursor_and_sends_the_contract_payload(self):
        register_sitemap_provider("fake_pages.sitemap_entries")
        collected = collect_entries("acme", "https://example.com")
        assert [u.loc for u in collected.urls] == [
            "https://example.com/about", "https://example.com/terms", "https://example.com/blog/hello",
        ]
        assert CALLS[0] == {"site": "acme", "locale": None, "cursor": None, "limit": 2}
        assert CALLS[1]["cursor"] == "2"
        about = collected.urls[0]
        assert about.alternates == [("en", "https://example.com/about"), ("de", "https://example.com/de/ueber")]
        assert collected.urls[2].lastmod is None
        assert collected.skipped == []

    def test_foreign_and_malformed_locations_dropped_and_duplicates_once(self):
        register_sitemap_provider("fake_pages.sitemap_entries")
        register_sitemap_provider("fake_listings.sitemap_entries")
        locs = [u.loc for u in collect_entries("acme", "https://example.com").urls]
        assert locs.count("https://example.com/about") == 1
        assert "https://example.com/l/1" in locs
        assert not any("evil" in loc for loc in locs)
        assert not any(loc.endswith("relative") for loc in locs)

    def test_unanswered_provider_skipped_and_logged_once(self, caplog):
        register_sitemap_provider("fake_broken.sitemap_entries")
        register_sitemap_provider("fake_pages.sitemap_entries")
        register_sitemap_provider("nobody.sitemap_entries", name="custom")
        with caplog.at_level(logging.WARNING, logger="stapel_core.django.sitemap.build"):
            collected = collect_entries("acme", "https://example.com")
        assert len(collected.urls) == 3
        assert sorted(collected.skipped) == ["custom", "fake_broken"]
        broken_lines = [r for r in caplog.records if "fake_broken" in r.getMessage()]
        assert len(broken_lines) == 1

    def test_malformed_answer_skipped(self):
        register_sitemap_provider("fake_garbage.sitemap_entries")
        collected = collect_entries("acme", "https://example.com")
        assert collected.urls == [] and collected.skipped == ["fake_garbage"]

    def test_repeated_cursor_stops_the_provider(self):
        register_sitemap_provider("fake_loop.sitemap_entries")
        collected = collect_entries("acme", "https://example.com")
        assert [u.loc for u in collected.urls] == ["https://example.com/loop/0", "https://example.com/loop/same"]
        assert collected.skipped == ["fake_loop"]

    def test_max_urls_truncates_and_says_so(self, caplog):
        register_sitemap_provider("fake_pages.sitemap_entries")
        with override_settings(STAPEL_SITEMAP={"BATCH_LIMIT": 2, "MAX_URLS": 2}):
            with caplog.at_level(logging.WARNING):
                collected = collect_entries("acme", "https://example.com")
        assert len(collected.urls) == 2 and collected.truncated
        assert "truncated at 2" in caplog.text

    def test_url_template_with_locale(self):
        register_sitemap_provider("fake_pages.sitemap_entries")
        with override_settings(STAPEL_SITEMAP={"URL_TEMPLATE": "{origin}/{locale}{path}"}):
            urls = collect_entries("acme", "https://example.com").urls
        assert urls[0].loc == "https://example.com/en/about"
        assert urls[0].alternates[1] == ("de", "https://example.com/de/de/ueber")


class TestRender:
    def test_page_split_by_url_count(self):
        urls = [Url(loc=f"https://example.com/{i}") for i in range(5)]
        with override_settings(STAPEL_SITEMAP={"PAGE_MAX_URLS": 2}):
            pages = render_pages(urls)
        assert [len(_locs(xml)) for xml, _ in pages] == [2, 2, 1]

    def test_page_split_by_bytes(self):
        urls = [Url(loc=f"https://example.com/{'x' * 100}{i}") for i in range(4)]
        with override_settings(STAPEL_SITEMAP={"PAGE_MAX_BYTES": 400}):
            pages = render_pages(urls)
        assert all(len(xml.encode()) <= 400 for xml, _ in pages)
        assert sum(len(_locs(xml)) for xml, _ in pages) == 4

    def test_escaping_and_non_ascii_paths(self):
        pages = render_pages([Url(loc="https://example.com/a?b=1&c=2")])
        assert "&amp;" in pages[0][0]
        ET.fromstring(pages[0][0])

    def test_empty_site_is_one_valid_empty_page(self):
        pages = render_pages([])
        assert len(pages) == 1 and _locs(pages[0][0]) == []


class TestViews:
    @pytest.fixture(autouse=True)
    def _settings(self, settings):
        settings.STAPEL_SITES = {"sites": [ACME, NORD]}
        settings.STAPEL_SITEMAP = {"PAGE_MAX_URLS": 2}

    def _get(self, view, host, path, **kwargs):
        request = rf.get(path, HTTP_HOST=host)
        return view.as_view()(request, **kwargs)

    def test_index_and_pages_for_the_request_site(self):
        register_sitemap_provider("fake_pages.sitemap_entries")
        index = self._get(SitemapIndexView, "www.example.com", "/sitemap.xml")
        assert index.status_code == 200
        assert index["Content-Type"].startswith("application/xml")
        assert "public" in index["Cache-Control"]
        root = ET.fromstring(index.content)
        assert root.tag == "{http://www.sitemaps.org/schemas/sitemap/0.9}sitemapindex"
        locs = [el.text for el in root.findall("sm:sitemap/sm:loc", NS)]
        assert locs == ["https://example.com/sitemap-1.xml", "https://example.com/sitemap-2.xml"]
        lastmods = [el.text for el in root.findall("sm:sitemap/sm:lastmod", NS)]
        assert lastmods == ["2026-11-05T10:00:00+00:00"]

        first = self._get(SitemapPageView, "example.com", "/sitemap-1.xml", page=1)
        assert _locs(first.content) == ["https://example.com/about", "https://example.com/terms"]
        links = ET.fromstring(first.content).findall("sm:url/xhtml:link", NS)
        assert {(link.get("hreflang"), link.get("href")) for link in links} == {
            ("en", "https://example.com/about"), ("de", "https://example.com/de/ueber"),
        }
        second = self._get(SitemapPageView, "example.com", "/sitemap-2.xml", page=2)
        assert _locs(second.content) == ["https://example.com/blog/hello"]

    def test_page_out_of_range_is_404(self):
        from django.http import Http404

        register_sitemap_provider("fake_pages.sitemap_entries")
        with pytest.raises(Http404):
            self._get(SitemapPageView, "example.com", "/sitemap-9.xml", page=9)

    def test_noindex_site_is_404(self):
        from django.http import Http404

        with pytest.raises(Http404):
            self._get(SitemapIndexView, "example.org", "/sitemap.xml")

    def test_cached_until_invalidated(self):
        register_sitemap_provider("fake_pages.sitemap_entries")
        self._get(SitemapIndexView, "example.com", "/sitemap.xml")
        calls = len(CALLS)
        self._get(SitemapIndexView, "example.com", "/sitemap.xml")
        self._get(SitemapPageView, "example.com", "/sitemap-1.xml", page=1)
        assert len(CALLS) == calls
        invalidate_sitemaps("acme")
        self._get(SitemapIndexView, "example.com", "/sitemap.xml")
        assert len(CALLS) > calls

    def test_partial_build_cached_shorter(self):
        register_sitemap_provider("fake_pages.sitemap_entries")
        register_sitemap_provider("fake_broken.sitemap_entries")
        built = build_sitemaps("acme", "https://example.com")
        assert not built.complete and built.skipped == ["fake_broken"]
        assert built.count == 2

    def test_urls_helper(self):
        names = [p.name for p in get_sitemap_urls()]
        assert names == ["stapel-sitemap-index", "stapel-sitemap-page"]


def test_single_host_deployment_uses_empty_site_key_and_origin_setting(settings):
    settings.STAPEL_SITES = {}
    settings.STAPEL_SITEMAP = {"ORIGIN": "https://single.example"}
    register_sitemap_provider("fake_pages.sitemap_entries")
    response = SitemapPageView.as_view()(rf.get("/sitemap-1.xml", HTTP_HOST="testserver"), page=1)
    assert _locs(response.content) == ["https://single.example/only"]
    assert CALLS[0]["site"] == ""
