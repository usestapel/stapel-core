"""The site registry: one build, N hosts (the multi-brand spec §3).

Two things are being defended here, and the second is the point.

1. The registry *parses* — every rule of the JSON shape is a loud
   :class:`SitesConfigError` and not a silently dropped site, because a site
   that quietly vanishes is a host that quietly stops being allowed.
2. The derivations agree. ``ALLOWED_HOSTS``, ``CSRF_TRUSTED_ORIGINS``, the
   WebSocket origin allowlist and the ``return_to`` gate all read the same
   registry; the failure mode this whole mechanism exists to prevent is four
   hand-maintained lists that disagree about one hostname.

``is_site_origin`` gets its own block: it is an open-redirect gate, and
``startswith`` is how those are lost.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest
from django.test import RequestFactory, override_settings

from stapel_core.django.jwt.ws_origin import configured_origins, websocket_origin_allowlist
from stapel_core.django.sites import site_for_request, site_frontend_url
from stapel_core.django.sites.checks import (
    E001_BAD_SITES,
    E002_PRIMARY_RULE,
    E003_COOKIE_DOMAIN_SPANS_SITES,
    W001_FRONTEND_URL_UNREGISTERED,
    check_cookie_domain_scope,
    check_frontend_url_host,
    check_sites_registry,
)
from stapel_core.django.sites.urls import get_site_urls
from stapel_core.django.sites.views import SiteBootstrapView
from stapel_core.sites import (
    Brand,
    Site,
    SiteRegistry,
    SitesConfigError,
    load_sites,
    registry_from_settings,
    reset_sites_cache,
    sites_data_from_env,
)
from stapel_core.django.api.permissions import ANONYMOUS_ALLOWED

ACME = {
    "host": "example.com",
    "aliases": ["www.example.com"],
    "primary": True,
    "locale": "ru",
    "brand": {
        "key": "acme",
        "name": "Acme",
        "title": "Acme — classifieds",
        "logo": "/brand/acme/logo.svg",
        "theme": "acme",
        "legal": {
            "company": "Acme Ltd",
            "support_email": "hello@example.com",
            "privacy_url": "/privacy",
            "terms_url": "https://example.com/terms",
        },
    },
    "seo": {"index": True},
}

NORD = {
    "host": "example.org",
    "aliases": ["www.example.org"],
    "primary": False,
    "locale": "ru",
    "brand": {
        "key": "nord",
        "name": "Nord",
        "title": "Nord — classifieds",
        "logo": "/brand/nord/logo.svg",
        "theme": "nord",
        "legal": {"company": "Nord Ltd", "support_email": "hello@example.org"},
    },
    "seo": {"index": True},
}

TWO_SITES = {"sites": [ACME, NORD]}

rf = RequestFactory()


@pytest.fixture(autouse=True)
def _clean_registry():
    """No cached registry, and no answer inherited from the developer's shell."""
    saved = {
        name: os.environ.pop(name, None)
        for name in ("STAPEL_SITES_FILE", "STAPEL_SITES_JSON")
    }
    reset_sites_cache()
    yield
    reset_sites_cache()
    for name, value in saved.items():
        if value is not None:
            os.environ[name] = value


# ---------------------------------------------------------------------------
# Parsing — the shape, and every rule that rejects one
# ---------------------------------------------------------------------------


class TestLoadSites:
    def test_two_sites_round_trip(self):
        registry = load_sites(TWO_SITES)
        assert [s.host for s in registry.sites] == ["example.com", "example.org"]
        acme = registry.sites[0]
        assert acme.aliases == ("www.example.com",)
        assert acme.primary is True
        assert acme.locale == "ru"
        assert isinstance(acme.brand, Brand)
        assert acme.brand.key == "acme"
        assert acme.brand.theme == "acme"
        assert acme.brand.legal["support_email"] == "hello@example.com"
        assert acme.seo["index"] is True

    def test_bare_list_is_accepted(self):
        """The setting may be the list itself — the file wraps it in an object."""
        assert len(load_sites([ACME])) == 1

    def test_empty_is_a_registry_not_an_error(self):
        """The single-host deployment declares nothing and keeps working."""
        for empty in (None, {}, [], {"sites": []}):
            registry = load_sites(empty)
            assert not registry
            assert registry.primary() is None
            assert registry.origins() == ()
            assert registry.for_host("example.com") is None

    def test_a_site_needs_no_brand(self):
        """An unbranded host is legal — the storefront keeps its fallback."""
        registry = load_sites({"sites": [{"host": "example.com"}]})
        assert registry.sites[0].brand is None

    def test_theme_defaults_to_the_brand_key(self):
        registry = load_sites({"sites": [{"host": "a.example", "brand": {"key": "k", "name": "K"}}]})
        assert registry.sites[0].brand.theme == "k"

    # -- rejections ---------------------------------------------------------

    def test_duplicate_host_across_sites(self):
        data = {"sites": [ACME, {**NORD, "host": "example.com"}]}
        with pytest.raises(SitesConfigError) as exc:
            load_sites(data)
        assert "example.com" in str(exc.value)
        assert exc.value.code == "duplicate"

    def test_alias_colliding_with_another_sites_host(self):
        data = {"sites": [ACME, {**NORD, "aliases": ["www.example.com"]}]}
        with pytest.raises(SitesConfigError) as exc:
            load_sites(data)
        assert "www.example.com" in str(exc.value)
        assert exc.value.code == "duplicate"

    def test_two_sites_and_no_primary(self):
        data = {"sites": [{**ACME, "primary": False}, NORD]}
        with pytest.raises(SitesConfigError) as exc:
            load_sites(data)
        assert exc.value.code == "primary"
        assert "primary" in str(exc.value)

    def test_two_primaries(self):
        data = {"sites": [ACME, {**NORD, "primary": True}]}
        with pytest.raises(SitesConfigError) as exc:
            load_sites(data)
        assert exc.value.code == "primary"
        assert "example.com" in str(exc.value) and "example.org" in str(exc.value)

    def test_a_lone_site_needs_no_primary_flag(self):
        registry = load_sites({"sites": [{**ACME, "primary": False}]})
        assert registry.primary().host == "example.com"

    @pytest.mark.parametrize("key", ["Acme", "acme_key", "бренд", "a.cme", ""])
    def test_brand_key_vocabulary(self, key):
        data = {"sites": [{"host": "a.example", "brand": {"key": key, "name": "x"}}]}
        with pytest.raises(SitesConfigError) as exc:
            load_sites(data)
        assert exc.value.code == "brand"

    @pytest.mark.parametrize("theme", ["Acme", "dark theme", "тема"])
    def test_brand_theme_vocabulary(self, theme):
        data = {"sites": [{"host": "a.example", "brand": {"key": "k", "name": "x", "theme": theme}}]}
        with pytest.raises(SitesConfigError) as exc:
            load_sites(data)
        assert exc.value.code == "brand"

    @pytest.mark.parametrize(
        "logo",
        ["http://cdn.example/logo.svg", "//cdn.example/logo.svg",
         "javascript:alert(1)", "brand/logo.svg"],
    )
    def test_logo_must_be_relative_or_https(self, logo):
        data = {"sites": [{"host": "a.example",
                           "brand": {"key": "k", "name": "x", "logo": logo}}]}
        with pytest.raises(SitesConfigError) as exc:
            load_sites(data)
        assert exc.value.code == "url"

    def test_legal_urls_must_be_relative_or_https(self):
        data = {"sites": [{"host": "a.example", "brand": {
            "key": "k", "name": "x", "legal": {"privacy_url": "http://a.example/p"}}}]}
        with pytest.raises(SitesConfigError) as exc:
            load_sites(data)
        assert exc.value.code == "url"

    def test_non_url_legal_lines_are_carried_verbatim(self):
        """``support_email`` is not a URL and must not be judged as one."""
        registry = load_sites({"sites": [{"host": "a.example", "brand": {
            "key": "k", "name": "x", "legal": {"support_email": "hi@a.example"}}}]})
        assert registry.sites[0].brand.legal["support_email"] == "hi@a.example"

    @pytest.mark.parametrize(
        "host", ["https://example.com", "example.com:8443", "example.com/path", "", "-example.com"]
    )
    def test_host_must_be_a_bare_hostname(self, host):
        with pytest.raises(SitesConfigError) as exc:
            load_sites({"sites": [{"host": host}]})
        assert exc.value.code == "host"

    def test_alias_must_be_a_bare_hostname(self):
        with pytest.raises(SitesConfigError) as exc:
            load_sites({"sites": [{"host": "a.example", "aliases": ["https://b.example"]}]})
        assert exc.value.code == "host"

    def test_aliases_must_be_a_list(self):
        with pytest.raises(SitesConfigError):
            load_sites({"sites": [{"host": "a.example", "aliases": "b.example"}]})

    def test_mapping_without_sites_key(self):
        with pytest.raises(SitesConfigError):
            load_sites({"hosts": ["example.com"]})

    def test_scalar_registry(self):
        with pytest.raises(SitesConfigError):
            load_sites("example.com")

    def test_site_entry_must_be_an_object(self):
        with pytest.raises(SitesConfigError):
            load_sites({"sites": ["example.com"]})

    def test_brand_must_be_an_object(self):
        with pytest.raises(SitesConfigError):
            load_sites({"sites": [{"host": "a.example", "brand": "acme"}]})

    def test_seo_must_be_an_object(self):
        with pytest.raises(SitesConfigError):
            load_sites({"sites": [{"host": "a.example", "seo": "index"}]})


# ---------------------------------------------------------------------------
# Matching — a Host header is not a canonical hostname
# ---------------------------------------------------------------------------


class TestForHost:
    @pytest.fixture
    def registry(self):
        return load_sites(TWO_SITES)

    @pytest.mark.parametrize(
        "sent", ["example.com", "EXAMPLE.COM", "example.com:8443", " example.com ", "example.com."]
    )
    def test_port_case_and_trailing_dot_are_noise(self, registry, sent):
        assert registry.for_host(sent).host == "example.com"

    def test_alias_resolves_to_its_site(self, registry):
        assert registry.for_host("www.example.org").host == "example.org"

    def test_unknown_host_is_none(self, registry):
        assert registry.for_host("attacker.test") is None
        assert registry.for_host("") is None
        assert registry.for_host(None) is None

    def test_primary_and_hosts_and_origins(self, registry):
        assert registry.primary().host == "example.com"
        assert registry.hosts() == ("example.com", "www.example.com", "example.org", "www.example.org")
        assert registry.origins() == (
            "https://example.com", "https://www.example.com",
            "https://example.org", "https://www.example.org",
        )


class TestIsSiteOrigin:
    """The open-redirect gate. Parsed, never prefix-matched."""

    @pytest.fixture
    def registry(self):
        return load_sites(TWO_SITES)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "https://example.com/",
            "https://example.com:443/l/75",
            "https://www.example.org/login?next=/l/75",
        ],
    )
    def test_admitted(self, registry, url):
        assert registry.is_site_origin(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com.attacker.test",       # startswith would have admitted it
            "https://example.com.attacker.test/x",
            "http://example.com",                # right host, wrong scheme
            "https://attacker.test/?x=example.com",    # merely mentions the host
            "https://attacker.test/#https://example.com",
            "https://example.com@attacker.test/",      # userinfo, not a host
            "https://attacker.test\\@example.com/",    # a browser reads \ as a separator
            "https://example.com:8443/",         # a non-default port is not us
            "//example.com/",                    # no scheme at all
            "/l/75",
            "javascript:alert(1)",
            "",
            None,
        ],
    )
    def test_refused(self, registry, url):
        assert registry.is_site_origin(url) is False

    def test_empty_registry_is_the_origin_of_nothing(self):
        assert SiteRegistry().is_site_origin("https://example.com") is False


# ---------------------------------------------------------------------------
# Loading from the environment
# ---------------------------------------------------------------------------


class TestEnvSources:
    def test_inline_json(self, monkeypatch):
        monkeypatch.setenv("STAPEL_SITES_JSON", json.dumps(TWO_SITES))
        assert load_sites(sites_data_from_env()).hosts()[0] == "example.com"

    def test_file(self, monkeypatch, tmp_path):
        path = tmp_path / "sites.json"
        path.write_text(json.dumps(TWO_SITES), encoding="utf-8")
        monkeypatch.setenv("STAPEL_SITES_FILE", str(path))
        assert len(load_sites(sites_data_from_env())) == 2

    def test_file_wins_over_inline(self, monkeypatch, tmp_path):
        path = tmp_path / "sites.json"
        path.write_text(json.dumps({"sites": [ACME]}), encoding="utf-8")
        monkeypatch.setenv("STAPEL_SITES_FILE", str(path))
        monkeypatch.setenv("STAPEL_SITES_JSON", json.dumps(TWO_SITES))
        assert len(load_sites(sites_data_from_env())) == 1

    def test_missing_file_is_loud(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STAPEL_SITES_FILE", str(tmp_path / "absent.json"))
        with pytest.raises(SitesConfigError):
            sites_data_from_env()

    def test_malformed_inline_json_is_loud(self, monkeypatch):
        monkeypatch.setenv("STAPEL_SITES_JSON", "{not json")
        with pytest.raises(SitesConfigError):
            sites_data_from_env()

    def test_nothing_declared_is_empty(self):
        assert sites_data_from_env({}) == {}

    def test_settings_win_over_env(self, monkeypatch):
        monkeypatch.setenv("STAPEL_SITES_JSON", json.dumps({"sites": [NORD]}))
        with override_settings(STAPEL_SITES={"sites": [ACME]}):
            assert registry_from_settings().hosts()[0] == "example.com"

    def test_env_is_read_when_the_setting_is_empty(self, monkeypatch):
        monkeypatch.setenv("STAPEL_SITES_JSON", json.dumps({"sites": [NORD]}))
        with override_settings(STAPEL_SITES={}):
            assert registry_from_settings().hosts()[0] == "example.org"

    def test_cache_is_per_process_and_resettable(self):
        with override_settings(STAPEL_SITES=TWO_SITES):
            first = registry_from_settings()
            assert registry_from_settings() is first
            reset_sites_cache()
            assert registry_from_settings() is not first


# ---------------------------------------------------------------------------
# django/settings.py — the derivation a project inherits by star-import
# ---------------------------------------------------------------------------

PROJECT_SETTINGS = """
from stapel_core.django.settings import *  # noqa: F401,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
]
ROOT_URLCONF = "projurls"
"""


def _boot(tmp_path, body, env=None):
    """Run *body* against a project settings module that star-imports ours.

    A settings module cannot be imported into a process that already has one,
    so the derivation is measured the way a real project gets it: a fresh
    interpreter, ``DJANGO_SETTINGS_MODULE``, ``django.setup()``.
    """
    (tmp_path / "projsettings.py").write_text(PROJECT_SETTINGS, encoding="utf-8")
    (tmp_path / "projurls.py").write_text("urlpatterns = []\n", encoding="utf-8")
    child_env = dict(os.environ)
    child_env["DJANGO_SETTINGS_MODULE"] = "projsettings"
    # tmp_path only: the repo root holds a ``django/`` package directory that
    # would shadow Django itself in the child interpreter.
    child_env["PYTHONPATH"] = str(tmp_path)
    for name in ("STAPEL_SITES_FILE", "STAPEL_SITES_JSON", "ALLOWED_HOSTS",
                 "CSRF_TRUSTED_ORIGINS", "STAPEL_HOST"):
        child_env.pop(name, None)
    child_env.update(env or {})
    script = "import django\ndjango.setup()\n" + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(tmp_path), env=child_env,
    )


class TestSettingsDerivation:
    def test_hosts_and_origins_derive_from_the_registry(self, tmp_path):
        result = _boot(tmp_path, """
            from django.conf import settings
            assert "example.com" in settings.ALLOWED_HOSTS, settings.ALLOWED_HOSTS
            assert "www.example.com" in settings.ALLOWED_HOSTS
            assert "example.org" in settings.ALLOWED_HOSTS
            assert "www.example.org" in settings.ALLOWED_HOSTS
            assert "https://example.org" in settings.CSRF_TRUSTED_ORIGINS
            assert "https://www.example.org" in settings.CSRF_TRUSTED_ORIGINS
            # extended, not replaced — and each name exactly once
            assert "localhost" in settings.ALLOWED_HOSTS
            assert len(settings.ALLOWED_HOSTS) == len(set(settings.ALLOWED_HOSTS))
            assert settings.STAPEL_SITES["sites"][0]["host"] == "example.com"
            print("OK")
        """, env={"STAPEL_SITES_JSON": json.dumps(TWO_SITES)})
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_operator_entries_survive(self, tmp_path):
        result = _boot(tmp_path, """
            from django.conf import settings
            assert settings.ALLOWED_HOSTS[0] == "healthcheck.internal"
            assert "example.com" in settings.ALLOWED_HOSTS
            print("OK")
        """, env={"STAPEL_SITES_JSON": json.dumps(TWO_SITES),
                  "ALLOWED_HOSTS": "healthcheck.internal"})
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_empty_registry_leaves_stapel_host_behaviour_untouched(self, tmp_path):
        result = _boot(tmp_path, """
            from django.conf import settings
            assert settings.STAPEL_SITES == {}, settings.STAPEL_SITES
            assert settings.ALLOWED_HOSTS == ["stg.example.com", "localhost", "127.0.0.1"]
            assert settings.CSRF_TRUSTED_ORIGINS == ["https://stg.example.com"]
            print("OK")
        """, env={"STAPEL_HOST": "stg.example.com"})
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_a_broken_registry_does_not_crash_the_import(self, tmp_path):
        """The settings module is the one place a system check cannot speak."""
        result = _boot(tmp_path, """
            from django.conf import settings
            # It booted at all — and single-host, because the registry did not
            # load. Nothing was invented from the broken declaration.
            assert "example.com" not in settings.ALLOWED_HOSTS, settings.ALLOWED_HOSTS
            from stapel_core.django.sites.checks import check_sites_registry
            findings = check_sites_registry()
            assert [f.id for f in findings] == ["stapel_core.sites.E002"], findings
            print("OK")
        """, env={"STAPEL_SITES_JSON": json.dumps(
            {"sites": [{**ACME, "primary": False}, NORD]})})
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Request → site
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_matched_host(self):
        with override_settings(STAPEL_SITES=TWO_SITES):
            request = rf.get("/", HTTP_HOST="example.org")
            assert site_for_request(request).host == "example.org"
            assert site_frontend_url(request, "https://fallback") == "https://example.org"

    def test_alias_matches_the_canonical_site(self):
        with override_settings(STAPEL_SITES=TWO_SITES):
            request = rf.get("/", HTTP_HOST="www.example.org")
            assert site_for_request(request).host == "example.org"
            assert site_frontend_url(request, "https://fallback") == "https://example.org"

    def test_unmatched_host_falls_back_to_primary_but_not_for_links(self):
        """A link is minted only for a host the registry actually recognises."""
        with override_settings(STAPEL_SITES=TWO_SITES):
            request = rf.get("/", HTTP_HOST="10.0.0.7")
            assert site_for_request(request).host == "example.com"
            assert site_frontend_url(request, "https://fallback") == "https://fallback"

    def test_empty_registry_is_none(self):
        with override_settings(STAPEL_SITES={}):
            request = rf.get("/", HTTP_HOST="example.com")
            assert site_for_request(request) is None
            assert site_frontend_url(request, "https://fallback") == "https://fallback"

    def test_a_broken_registry_degrades_instead_of_500ing_every_page(self):
        """Settings already decided "single-host"; the request path must agree."""
        with override_settings(STAPEL_SITES={"sites": [ACME, ACME]}):
            request = rf.get("/", HTTP_HOST="example.com")
            assert site_for_request(request) is None
            assert site_frontend_url(request, "https://fallback") == "https://fallback"
            assert _bootstrap("example.com").data["brand"] is None
            # ...and it is still an E-level finding, not a swallowed error.
            assert [f.id for f in check_sites_registry()] == [E001_BAD_SITES]


# ---------------------------------------------------------------------------
# GET …/site/ — the storefront bootstrap
# ---------------------------------------------------------------------------


def _bootstrap(host):
    return SiteBootstrapView.as_view()(rf.get("/site/", HTTP_HOST=host))


class TestBootstrapView:
    def test_matched_host(self):
        with override_settings(STAPEL_SITES=TWO_SITES):
            response = _bootstrap("example.org")
        assert response.status_code == 200
        assert response["Cache-Control"] == "public, max-age=300"
        body = response.data
        assert body["host"] == "example.org"
        assert body["matched"] is True
        assert body["primary"] is False
        assert body["locale"] == "ru"
        assert body["brand"]["key"] == "nord"
        assert body["brand"]["theme"] == "nord"
        assert body["brand"]["logo"] == "/brand/nord/logo.svg"
        assert body["brand"]["legal"]["support_email"] == "hello@example.org"
        assert body["seo"] == {"index": True, "canonical_host": "example.org"}

    def test_alias_reports_the_canonical_host(self):
        """``www`` is one cookie jurisdiction with the apex, and one canonical."""
        with override_settings(STAPEL_SITES=TWO_SITES):
            body = _bootstrap("www.example.org").data
        assert body["host"] == "example.org"
        assert body["matched"] is True
        assert body["seo"]["canonical_host"] == "example.org"

    def test_declared_canonical_host_is_honoured(self):
        data = {"sites": [{**ACME, "seo": {"index": False, "canonical_host": "example.com"}}]}
        with override_settings(STAPEL_SITES=data):
            body = _bootstrap("www.example.com").data
        assert body["seo"] == {"index": False, "canonical_host": "example.com"}

    def test_unknown_host_falls_back_to_primary_and_says_so(self):
        with override_settings(STAPEL_SITES=TWO_SITES):
            body = _bootstrap("10.0.0.7").data
        assert body["matched"] is False
        assert body["primary"] is True
        assert body["host"] == "example.com"
        assert body["brand"]["key"] == "acme"

    def test_empty_registry_reports_the_request_host_and_no_brand(self):
        with override_settings(STAPEL_SITES={}):
            body = _bootstrap("example.com").data
        assert body == {
            "host": "example.com",
            "matched": False,
            "primary": False,
            "locale": body["locale"],
            "brand": None,
            "seo": {"index": True, "canonical_host": "example.com"},
        }

    def test_it_is_reachable_without_any_credential(self):
        """No authentication class runs: a stale cookie must not 401 the brand."""
        assert SiteBootstrapView.authentication_classes == []
        names = [c.__name__ for c in SiteBootstrapView.permission_classes]
        assert names == ["AllowAny"]

    def test_it_declares_its_stance_on_guests(self):
        assert SiteBootstrapView.stapel_anonymous_access == ANONYMOUS_ALLOWED

    def test_the_route_is_the_same_address_in_every_fleet(self):
        urls = get_site_urls()
        assert len(urls) == 1
        assert str(urls[0].pattern) == "site/"
        assert urls[0].name == "site-bootstrap"


# ---------------------------------------------------------------------------
# System checks
# ---------------------------------------------------------------------------


class TestChecks:
    def test_silent_without_a_registry(self):
        with override_settings(STAPEL_SITES={}):
            assert check_sites_registry() == []
            assert check_cookie_domain_scope() == []
            assert check_frontend_url_host() == []

    def test_silent_on_a_healthy_registry(self):
        with override_settings(STAPEL_SITES=TWO_SITES, JWT_COOKIE_DOMAIN=None,
                               STAPEL_AUTH={"FRONTEND_URL": "https://example.com"}):
            assert check_sites_registry() == []
            assert check_cookie_domain_scope() == []
            assert check_frontend_url_host() == []

    def test_e001_malformed_registry(self):
        with override_settings(STAPEL_SITES={"sites": [ACME, ACME]}):
            findings = check_sites_registry()
        assert [f.id for f in findings] == [E001_BAD_SITES]
        assert "example.com" in findings[0].msg

    def test_e002_primary_rule(self):
        with override_settings(STAPEL_SITES={"sites": [{**ACME, "primary": False}, NORD]}):
            findings = check_sites_registry()
        assert [f.id for f in findings] == [E002_PRIMARY_RULE]

    def test_e003_cookie_domain_spanning_two_registrable_domains(self):
        with override_settings(STAPEL_SITES=TWO_SITES, JWT_COOKIE_DOMAIN=".example.com"):
            findings = check_cookie_domain_scope()
        assert [f.id for f in findings] == [E003_COOKIE_DOMAIN_SPANS_SITES]
        message = findings[0].msg
        assert "Domain=" in message
        assert "registrable domain" in message
        assert "cookie-tossing" in message

    def test_e003_is_silent_within_one_registrable_domain(self):
        """``www.example.com`` + ``example.com`` is one jurisdiction, not two."""
        with override_settings(STAPEL_SITES={"sites": [ACME]},
                               JWT_COOKIE_DOMAIN=".example.com"):
            assert check_cookie_domain_scope() == []

    def test_e003_is_silent_for_host_only_cookies(self):
        with override_settings(STAPEL_SITES=TWO_SITES, JWT_COOKIE_DOMAIN=None):
            assert check_cookie_domain_scope() == []

    def test_e003_cannot_be_silenced_wholesale(self):
        from stapel_core.django.check_guard import is_security_critical

        assert is_security_critical(E003_COOKIE_DOMAIN_SPANS_SITES)

    def test_w001_frontend_url_off_the_registry(self):
        with override_settings(STAPEL_SITES=TWO_SITES,
                               STAPEL_AUTH={"FRONTEND_URL": "https://old.example.com"}):
            findings = check_frontend_url_host()
        assert [f.id for f in findings] == [W001_FRONTEND_URL_UNREGISTERED]
        assert findings[0].level < 40  # Warning: staging legitimately differs

    def test_w001_accepts_an_alias(self):
        with override_settings(STAPEL_SITES=TWO_SITES,
                               STAPEL_AUTH={"FRONTEND_URL": "https://www.example.com"}):
            assert check_frontend_url_host() == []

    def test_w001_is_silent_when_the_setting_is_absent(self):
        with override_settings(STAPEL_SITES=TWO_SITES, STAPEL_AUTH={}):
            assert check_frontend_url_host() == []


# ---------------------------------------------------------------------------
# The WebSocket origin allowlist
# ---------------------------------------------------------------------------


class TestWebsocketOrigins:
    def test_site_origins_join_the_allowlist(self):
        with override_settings(STAPEL_SITES=TWO_SITES, STAPEL_WS_ALLOWED_ORIGINS=[]):
            allowed = websocket_origin_allowlist()
        assert "https://example.org" in allowed
        assert "https://www.example.com" in allowed

    def test_the_explicit_setting_is_added_to_not_replaced(self):
        """A dev server is not a site; a site is not written twice."""
        with override_settings(STAPEL_SITES=TWO_SITES,
                               STAPEL_WS_ALLOWED_ORIGINS=["http://localhost:5173"]):
            entries = configured_origins()
            allowed = websocket_origin_allowlist()
        assert entries[0] == "http://localhost:5173"
        assert "https://example.com" in entries
        assert len(entries) == len(set(entries))
        assert "http://localhost:5173" in allowed
        assert "https://example.com" in allowed

    def test_still_fails_closed_with_neither(self):
        with override_settings(STAPEL_SITES={}, STAPEL_WS_ALLOWED_ORIGINS=[]):
            assert configured_origins() == []
            assert websocket_origin_allowlist() == set()

    def test_a_broken_registry_never_widens_the_allowlist(self):
        with override_settings(STAPEL_SITES={"sites": [ACME, ACME]},
                               STAPEL_WS_ALLOWED_ORIGINS=["https://a.example"]):
            assert configured_origins() == ["https://a.example"]


# ---------------------------------------------------------------------------
# Dataclass surface (the shape the other slices import)
# ---------------------------------------------------------------------------


def test_site_and_brand_are_frozen():
    site = load_sites({"sites": [ACME]}).sites[0]
    with pytest.raises(Exception):
        site.host = "attacker.test"
    with pytest.raises(Exception):
        site.brand.key = "evil"
    assert isinstance(site, Site)
    assert site.hosts == ("example.com", "www.example.com")
    assert site.origins == ("https://example.com", "https://www.example.com")
