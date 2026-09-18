"""The contract check (tag ``stapel_contract``): a route the schema cannot see.

The defect this closes is a green gate over an invisible route. drf-spectacular
builds a service's ``docs/schema.json`` from its DRF views; a plain Django
``View`` under the API prefix is simply skipped, so the route is live, polled by
clients, and absent from the document every consumer generates against — and
the contract gate passes because it never knew the route existed.

The finding is "a route the contract cannot see", never "rewrite this view":
an SSE stream, a file download or a webhook receiver may legitimately be a
plain view, and says so with ``stapel_contract_exempt``.
"""
from django.core import checks as django_checks
from django.test import override_settings

from stapel_core.django.contract_checks import (
    CONTRACT_EXEMPT_ATTR,
    W001_API_ROUTE_INVISIBLE,
    W002_BAD_CONTRACT_EXEMPTION,
    check_api_routes_are_drf_views,
)

URLS = "tests.contract_surface_urls"


def _msgs(findings, check_id):
    return [f.msg for f in findings if f.id == check_id]


class TestTheInvisibleRoute:
    @override_settings(ROOT_URLCONF=URLS)
    def test_a_plain_view_under_the_api_prefix_is_named(self):
        findings = check_api_routes_are_drf_views()
        reported = _msgs(findings, W001_API_ROUTE_INVISIBLE)
        assert any("PlainStatusView" in m for m in reported)
        assert any("plain_function_view" in m for m in reported)
        assert all(
            isinstance(f, django_checks.Warning)
            for f in findings if f.id == W001_API_ROUTE_INVISIBLE
        )

    @override_settings(ROOT_URLCONF=URLS)
    def test_the_path_is_in_the_message(self):
        reported = _msgs(check_api_routes_are_drf_views(), W001_API_ROUTE_INVISIBLE)
        assert any("api/v1/plain/" in m for m in reported)

    @override_settings(ROOT_URLCONF=URLS)
    def test_one_view_mounted_twice_is_reported_once(self):
        reported = _msgs(check_api_routes_are_drf_views(), W001_API_ROUTE_INVISIBLE)
        assert sum("PlainStatusView" in m for m in reported) == 1

    @override_settings(ROOT_URLCONF=URLS)
    def test_a_drf_view_is_silent(self):
        reported = " ".join(_msgs(check_api_routes_are_drf_views(),
                                  W001_API_ROUTE_INVISIBLE))
        assert "ProperAPIView" not in reported
        assert "proper_function_view" not in reported

    @override_settings(ROOT_URLCONF=URLS)
    def test_a_plain_view_outside_the_api_prefix_is_silent(self):
        reported = " ".join(m for m in
                            _msgs(check_api_routes_are_drf_views(),
                                  W001_API_ROUTE_INVISIBLE))
        assert "FrontendPageView" not in reported

    @override_settings(ROOT_URLCONF=URLS)
    def test_an_exempt_view_is_silent(self):
        reported = " ".join(_msgs(check_api_routes_are_drf_views(),
                                  W001_API_ROUTE_INVISIBLE))
        assert "StreamingExemptView" not in reported
        assert "BareExemptView" not in reported

    @override_settings(ROOT_URLCONF=URLS)
    def test_an_exemption_that_is_not_one_does_not_read_as_a_declaration(self):
        findings = check_api_routes_are_drf_views()
        malformed = _msgs(findings, W002_BAD_CONTRACT_EXEMPTION)
        assert any("TypoedExemptionView" in m for m in malformed)
        assert CONTRACT_EXEMPT_ATTR in malformed[0]


class TestWithoutAUrlconf:
    @override_settings(ROOT_URLCONF="")
    def test_no_urlconf_no_findings(self):
        assert check_api_routes_are_drf_views() == []


class TestTheRouteThatMotivatedTheCheck:
    @override_settings(ROOT_URLCONF="tests.jwt_status_urls")
    def test_jwt_status_is_clean_now(self):
        """The view this check was written for must itself be green."""
        assert check_api_routes_are_drf_views() == []


class TestTheDecorator:
    def test_it_marks_a_function_view(self):
        from stapel_core.django.contract_checks import contract_exempt

        @contract_exempt("a stream")
        def view(request):  # pragma: no cover - never called
            return None

        assert getattr(view, CONTRACT_EXEMPT_ATTR) == "a stream"

    def test_it_refuses_a_reason_that_is_not_one(self):
        import pytest

        from stapel_core.django.contract_checks import contract_exempt

        with pytest.raises(ValueError):
            contract_exempt("")


class TestCoresOwnProbeSurface:
    """Health, readiness, liveness, metrics and version are plain views on
    purpose — every service mounts them, so if they were silent findings this
    check would arrive with a five-line flood and be muted on its first day."""

    @override_settings(ROOT_URLCONF="tests.health_surface_urls")
    def test_the_probe_surface_declares_itself(self):
        assert check_api_routes_are_drf_views() == []
