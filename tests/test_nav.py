"""Navigation registries — admin-suite AS-4 (stapel_core.django.nav)."""
import pytest

from stapel_core.django import nav
from stapel_core.django.nav import (
    NavConfigError,
    build_services,
    current_dashboard_url,
    get_nav_links,
    get_services,
    nav_sections,
    register_nav_link,
)


@pytest.fixture(autouse=True)
def _clean_registry(settings):
    # Isolate the code-channel registry and clear any inherited overlay.
    nav.clear_nav_links()
    settings.STAPEL_ADMIN = {"NAV_LINKS": {}}
    settings.STAPEL_SERVICES = None
    settings.URL_PREFIX = ""
    yield
    nav.clear_nav_links()


class _User:
    def __init__(self, *, staff=True, superuser=False, auth=True):
        self.is_authenticated = auth
        self.is_staff = staff
        self.is_superuser = superuser


# ─── STAPEL_SERVICES ────────────────────────────────────────────────────────


class TestServices:
    def test_env_json_string(self, settings):
        settings.STAPEL_SERVICES = (
            '[{"name": "Auth", "prefix": "auth"}, '
            '{"name": "Billing", "prefix": "billing/"}]'
        )
        services = get_services()
        assert [(s.name, s.prefix) for s in services] == [
            ("Auth", "auth"),
            ("Billing", "billing"),  # trailing slash normalized
        ]

    def test_setting_list(self, settings):
        settings.STAPEL_SERVICES = [{"name": "Auth", "prefix": "auth"}]
        assert get_services()[0].name == "Auth"

    def test_monolith_fallback_unset(self, settings):
        settings.STAPEL_SERVICES = None
        settings.URL_PREFIX = "shop/"
        services = get_services()
        assert len(services) == 1
        assert services[0].prefix == "shop"
        assert services[0].name == "Shop"

    def test_monolith_fallback_root(self, settings):
        settings.STAPEL_SERVICES = None
        settings.URL_PREFIX = ""
        services = get_services()
        assert len(services) == 1
        assert services[0].prefix == ""
        assert services[0].name == "This service"

    def test_bad_json_raises(self, settings):
        settings.STAPEL_SERVICES = "{not json"
        with pytest.raises(NavConfigError):
            get_services()

    def test_non_list_raises(self, settings):
        settings.STAPEL_SERVICES = '{"name": "x"}'
        with pytest.raises(NavConfigError):
            get_services()

    def test_missing_prefix_raises(self, settings):
        settings.STAPEL_SERVICES = '[{"name": "Auth"}]'
        with pytest.raises(NavConfigError):
            get_services()


class TestBuildServices:
    def test_urls_and_active(self, settings):
        settings.STAPEL_SERVICES = [
            {"name": "Auth", "prefix": "auth"},
            {"name": "Shop", "prefix": "shop"},
        ]
        settings.URL_PREFIX = "shop/"
        built = build_services(include_swagger=True)
        auth, shop = built
        assert auth["admin_url"] == "/auth/admin/"
        assert auth["swagger_url"] == "/auth/swagger/"
        assert auth["is_active"] is False
        assert shop["is_active"] is True

    def test_swagger_gated_off(self, settings):
        settings.STAPEL_SERVICES = [{"name": "Auth", "prefix": "auth"}]
        built = build_services(include_swagger=False)
        assert built[0]["swagger_url"] is None

    def test_swagger_auto_detect_off_without_urlconf(self, settings):
        # No ROOT_URLCONF in the harness → reverse('swagger-ui') fails.
        settings.STAPEL_SERVICES = [{"name": "Auth", "prefix": "auth"}]
        built = build_services()
        assert built[0]["swagger_url"] is None
        assert nav.swagger_mounted() is False


# ─── NAV_LINKS merge-registry ───────────────────────────────────────────────


class TestNavLinksRegistry:
    def test_code_channel(self):
        register_nav_link(
            "translate.dashboard", section="tools",
            title="Translator Dashboard", url="/translate/dashboard/",
        )
        links = get_nav_links()
        assert len(links) == 1
        assert links[0].section == "tools"
        assert links[0].requires == "staff"
        assert links[0].service_dashboard is False  # default off

    def test_code_channel_service_dashboard_flag(self):
        register_nav_link(
            "translate.dashboard", section="dashboards",
            title="Translator Dashboard", url="/translate/dashboard/",
            service_dashboard=True,
        )
        assert get_nav_links()[0].service_dashboard is True

    def test_settings_add(self, settings):
        settings.STAPEL_ADMIN = {"NAV_LINKS": {
            "monitoring.grafana": {
                "section": "monitoring", "title": "Grafana",
                "url": "/monitoring/grafana/", "external": True,
            },
        }}
        links = get_nav_links()
        assert links[0].external is True
        assert links[0].section == "monitoring"

    def test_settings_patch_over_code(self, settings):
        register_nav_link(
            "translate.dashboard", section="tools",
            title="Translator Dashboard", url="/translate/dashboard/",
        )
        settings.STAPEL_ADMIN = {"NAV_LINKS": {
            "translate.dashboard": {"title": "Переводы"},  # partial patch
        }}
        link = get_nav_links()[0]
        assert link.title == "Переводы"
        assert link.url == "/translate/dashboard/"  # inherited from code

    def test_settings_none_removes(self, settings):
        register_nav_link(
            "translate.dashboard", section="tools",
            title="Translator Dashboard", url="/translate/dashboard/",
        )
        settings.STAPEL_ADMIN = {"NAV_LINKS": {"translate.dashboard": None}}
        assert get_nav_links() == []

    def test_partial_patch_without_base_raises(self, settings):
        settings.STAPEL_ADMIN = {"NAV_LINKS": {"x": {"title": "orphan"}}}
        with pytest.raises(NavConfigError):
            get_nav_links()

    def test_unknown_section_code_raises(self):
        with pytest.raises(NavConfigError):
            register_nav_link("x", section="bogus", title="X", url="/x/")

    def test_unknown_requires_code_raises(self):
        with pytest.raises(NavConfigError):
            register_nav_link(
                "x", section="tools", title="X", url="/x/", requires="root"
            )

    def test_unknown_key_in_overlay_raises(self, settings):
        settings.STAPEL_ADMIN = {"NAV_LINKS": {"x": {
            "section": "tools", "title": "X", "url": "/x/", "bogus": 1,
        }}}
        with pytest.raises(NavConfigError):
            get_nav_links()


class TestNavSections:
    def _register_some(self):
        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/")
        register_nav_link("m.graf", section="monitoring", title="Grafana",
                          url="https://grafana.example/", requires="superuser",
                          external=True)

    def test_staff_sees_staff_links_only(self):
        self._register_some()
        sections = nav_sections(_User(staff=True, superuser=False))
        assert "tools" in sections
        assert "monitoring" not in sections  # requires superuser

    def test_superuser_sees_all(self):
        self._register_some()
        sections = nav_sections(_User(staff=True, superuser=True))
        assert set(sections) == {"tools", "monitoring"}
        assert sections["monitoring"][0]["external"] is True
        assert sections["monitoring"][0]["url"] == "https://grafana.example/"

    def test_non_staff_sees_nothing(self):
        self._register_some()
        assert nav_sections(_User(staff=False)) == {}

    def test_anonymous_sees_nothing(self):
        self._register_some()
        assert nav_sections(_User(auth=False)) == {}

    def test_empty_registry(self):
        assert nav_sections(_User()) == {}

    def test_internal_url_script_prefixed(self):
        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/")
        sections = nav_sections(_User())
        # get_script_prefix() is "/" in the harness → unchanged.
        assert sections["tools"][0]["url"] == "/translate/dashboard/"


class TestCurrentDashboard:
    def test_matches_current_service(self, settings):
        settings.URL_PREFIX = "translate/"
        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/")
        register_nav_link("o.dash", section="tools", title="Other",
                          url="/other/dashboard/")
        assert current_dashboard_url(_User()) == "/translate/dashboard/"

    def test_none_when_no_local_dashboard(self, settings):
        settings.URL_PREFIX = "auth/"
        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/")
        assert current_dashboard_url(_User()) is None

    def test_flag_wins_over_prefix_heuristic(self, settings):
        # The prefix heuristic alone would pick "o.dash" (it lives under the
        # current prefix); the explicit flag on "t.dash" must win instead.
        settings.URL_PREFIX = "other/"
        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/", service_dashboard=True)
        register_nav_link("o.dash", section="tools", title="Other",
                          url="/other/dashboard/")
        assert current_dashboard_url(_User()) == "/translate/dashboard/"

    def test_flag_via_settings_overlay(self, settings):
        settings.URL_PREFIX = "other/"
        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/")
        register_nav_link("o.dash", section="tools", title="Other",
                          url="/other/dashboard/")
        settings.STAPEL_ADMIN = {"NAV_LINKS": {
            "t.dash": {"service_dashboard": True},
        }}
        assert current_dashboard_url(_User()) == "/translate/dashboard/"

    def test_fallback_heuristic_when_no_flag_set(self, settings):
        # No link carries service_dashboard — old prefix-matching behavior.
        settings.URL_PREFIX = "translate/"
        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/")
        assert current_dashboard_url(_User()) == "/translate/dashboard/"

    def test_flag_ignores_prefix_mismatch(self, settings):
        # A flagged link wins even when it does not sit under the current
        # prefix at all (the module owns its own dashboard regardless).
        settings.URL_PREFIX = "auth/"
        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/", service_dashboard=True)
        assert current_dashboard_url(_User()) == "/translate/dashboard/"

    def test_flag_respects_admissibility_gate(self, settings):
        settings.URL_PREFIX = "translate/"
        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/", service_dashboard=True,
                          requires="superuser")
        # Not a superuser — the flagged link is inadmissible, so it must not
        # be returned (and there is no fallback candidate here either).
        assert current_dashboard_url(_User(superuser=False)) is None

    def test_flag_ignored_outside_dashboard_sections(self, settings):
        # service_dashboard on a "monitoring" link is not a dashboard link at
        # all — the section restriction still applies to the flagged branch.
        register_nav_link("m.graf", section="monitoring", title="Grafana",
                          url="https://grafana.example/", external=True,
                          service_dashboard=True)
        assert current_dashboard_url(_User()) is None

    def test_two_flags_first_in_order_wins(self, settings):
        register_nav_link("a.dash", section="tools", title="A",
                          url="/a/dashboard/", service_dashboard=True)
        register_nav_link("b.dash", section="tools", title="B",
                          url="/b/dashboard/", service_dashboard=True)
        assert current_dashboard_url(_User()) == "/a/dashboard/"


class TestServiceDashboardDuplicateCheck:
    def test_no_flags_clean(self, settings):
        from stapel_core.django.nav_checks import check_service_dashboard_duplicates

        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/")
        assert check_service_dashboard_duplicates() == []

    def test_single_flag_clean(self, settings):
        from stapel_core.django.nav_checks import check_service_dashboard_duplicates

        register_nav_link("t.dash", section="tools", title="Dash",
                          url="/translate/dashboard/", service_dashboard=True)
        assert check_service_dashboard_duplicates() == []

    def test_duplicate_flags_warn(self, settings):
        from stapel_core.django.nav_checks import (
            W003_DUPLICATE_SERVICE_DASHBOARD,
            check_service_dashboard_duplicates,
        )

        register_nav_link("a.dash", section="tools", title="A",
                          url="/a/dashboard/", service_dashboard=True)
        register_nav_link("b.dash", section="tools", title="B",
                          url="/b/dashboard/", service_dashboard=True)
        warnings = check_service_dashboard_duplicates()
        assert any(w.id == W003_DUPLICATE_SERVICE_DASHBOARD for w in warnings)


# ─── system checks ──────────────────────────────────────────────────────────


class TestChecks:
    def test_bad_services_flagged(self, settings):
        from stapel_core.django.nav_checks import (
            E001_BAD_SERVICES,
            check_services,
        )

        settings.STAPEL_SERVICES = "{not json"
        errors = check_services()
        assert any(e.id == E001_BAD_SERVICES for e in errors)

    def test_good_services_clean(self, settings):
        from stapel_core.django.nav_checks import check_services

        settings.STAPEL_SERVICES = '[{"name": "Auth", "prefix": "auth"}]'
        assert check_services() == []

    def test_bad_nav_links_flagged(self, settings):
        from stapel_core.django.nav_checks import (
            E002_BAD_NAV_LINKS,
            check_nav_links,
        )

        settings.STAPEL_ADMIN = {"NAV_LINKS": {"x": {"title": "orphan"}}}
        errors = check_nav_links()
        assert any(e.id == E002_BAD_NAV_LINKS for e in errors)
