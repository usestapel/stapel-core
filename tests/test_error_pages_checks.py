"""System check (tag ``stapel_error_pages``) — API urls without the
JSON error-page middleware installed."""
from django.test import override_settings

from stapel_core.django.api.error_pages import MIDDLEWARE_PATH
from stapel_core.django.error_pages_checks import (
    W001_MIDDLEWARE_ABSENT,
    check_api_error_pages_middleware,
)

WITH_API = "tests.error_pages_urls"
WITHOUT_API = "tests.error_pages_no_api_urls"

OTHER_MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
WITH_GATE = [MIDDLEWARE_PATH, "django.middleware.common.CommonMiddleware"]


def _ids(findings):
    return [f.id for f in findings]


@override_settings(ROOT_URLCONF=WITH_API, MIDDLEWARE=OTHER_MIDDLEWARE)
def test_warns_when_api_urls_exist_and_middleware_absent():
    findings = check_api_error_pages_middleware()
    assert _ids(findings) == [W001_MIDDLEWARE_ABSENT]


@override_settings(ROOT_URLCONF=WITH_API, MIDDLEWARE=WITH_GATE)
def test_silent_when_middleware_installed():
    assert check_api_error_pages_middleware() == []


@override_settings(ROOT_URLCONF=WITHOUT_API, MIDDLEWARE=OTHER_MIDDLEWARE)
def test_silent_when_no_api_surface_at_all():
    """No API urls at all — the premise is false, regardless of MIDDLEWARE."""
    assert check_api_error_pages_middleware() == []


@override_settings(ROOT_URLCONF="", MIDDLEWARE=OTHER_MIDDLEWARE)
def test_silent_in_standalone_package_harness():
    """No ROOT_URLCONF (this repo's own test harness) — nothing to survey."""
    assert check_api_error_pages_middleware() == []
