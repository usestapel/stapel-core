"""ApiErrorPagesMiddleware — JSON envelopes for a 404/405 no DRF view saw.

Behavior under test:
  * an unknown path under an API prefix answers the fleet's JSON envelope;
  * an unknown path OUTSIDE an API prefix keeps Django's HTML default;
  * a 405 (wrong verb) on a plain, non-DRF API view is also converted, and
    its Allow header survives the conversion;
  * STAPEL_CORE["API_PREFIXES"] is genuinely read, not hardcoded.
"""
from django.test import Client, override_settings

from stapel_core.django.api.error_pages import (
    HANDLED_STATUSES,
    MIDDLEWARE_PATH,
    is_api_path,
)

URLS = "tests.error_pages_urls"

MIDDLEWARE = [MIDDLEWARE_PATH, "django.middleware.common.CommonMiddleware"]


class TestIsApiPath:
    def test_default_prefix_matches_anywhere_in_path(self):
        assert is_api_path("/search/api/v1/facets")
        assert is_api_path("api/v1/facets")  # no leading slash — still matched

    def test_default_prefix_rejects_non_api_path(self):
        assert not is_api_path("/search/dashboard/")

    @override_settings(STAPEL_CORE={"API_PREFIXES": ["/custom/"]})
    def test_prefix_config_is_read_not_hardcoded(self):
        assert is_api_path("/svc/custom/thing")
        assert not is_api_path("/svc/api/v1/thing")  # default no longer applies


class TestUnknownApiPath:
    @override_settings(ROOT_URLCONF=URLS, MIDDLEWARE=MIDDLEWARE)
    def test_unknown_api_path_answers_json_404(self):
        response = Client().get("/search/api/v1/does-not-exist/")
        assert response.status_code == 404
        assert response["Content-Type"].startswith("application/json")
        body = response.json()
        assert body["localizable_error"] == "error.404.not_found"

    @override_settings(ROOT_URLCONF=URLS, MIDDLEWARE=MIDDLEWARE)
    def test_unknown_non_api_path_keeps_html(self):
        response = Client().get("/search/dashboard/does-not-exist/")
        assert response.status_code == 404
        assert "html" in response["Content-Type"].lower()


class TestMethodNotAllowed:
    @override_settings(ROOT_URLCONF=URLS, MIDDLEWARE=MIDDLEWARE)
    def test_405_on_api_path_answers_json(self):
        response = Client().post("/search/api/v1/things/")
        assert response.status_code == 405
        assert response["Content-Type"].startswith("application/json")
        body = response.json()
        assert body["localizable_error"] == "error.405.method_not_allowed"

    @override_settings(ROOT_URLCONF=URLS, MIDDLEWARE=MIDDLEWARE)
    def test_allow_header_is_preserved(self):
        response = Client().post("/search/api/v1/things/")
        assert response.get("Allow") == "GET"


class TestPrefixConfigRespectedEndToEnd:
    @override_settings(
        ROOT_URLCONF=URLS,
        MIDDLEWARE=MIDDLEWARE,
        STAPEL_CORE={"API_PREFIXES": ["/search/"]},
    )
    def test_widened_prefix_also_converts_dashboard_404(self):
        """/search/dashboard/... now falls under the widened prefix too."""
        response = Client().get("/search/dashboard/does-not-exist/")
        assert response.status_code == 404
        assert response["Content-Type"].startswith("application/json")


def test_handled_statuses_are_exactly_404_and_405():
    assert set(HANDLED_STATUSES) == {404, 405}
