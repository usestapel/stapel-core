"""``/api/metrics/`` carries each series once, whoever registered it twice.

Motivating incident: eight services in a fleet each exported
``stapel_schema_probe_ok`` and ``stapel_schema_at_head`` twice. Core's
``CommonDjangoConfig.ready()`` registered the collector this package ships,
and each product still carried the per-service copy the package was lifted
from — a *different* function object emitting the *same* series, which no
registry can tell apart. Prometheus logged ``Error on ingesting samples with
different value but same timestamp`` and dropped one sample per pair, ~164
times in two hours. Both copies happened to read 1, so nothing looked broken;
which copy survived was Prometheus' choice, and two alert rules read exactly
those series.
"""
import logging

import pytest
from django.test import RequestFactory, override_settings

from stapel_core.django.monitoring import health as health_mod


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(health_mod, "_custom_metrics_exporters", [])
    monkeypatch.setattr(health_mod, "_dependency_checks", [])


def _scrape():
    response = health_mod.prometheus_metrics(RequestFactory().get("/api/metrics/"))
    return response.content.decode()


# ---------------------------------------------------------------------------
# the incident, reproduced
# ---------------------------------------------------------------------------


def _library_copy():
    return (
        '# HELP stapel_schema_probe_ok Whether the schema state could be determined\n'
        '# TYPE stapel_schema_probe_ok gauge\n'
        'stapel_schema_probe_ok{service="iron_auth"} 1\n'
        '# HELP stapel_schema_at_head Whether the schema is at the code\'s head\n'
        '# TYPE stapel_schema_at_head gauge\n'
        'stapel_schema_at_head{service="iron_auth"} 1'
    )


def _product_leftover_copy():
    """Byte-identical output from a different function — the real shape."""
    return _library_copy()


@override_settings(SERVICE_NAME="iron_auth")
def test_two_collectors_of_one_series_expose_it_once(clean_registry):
    health_mod.register_metrics_exporter(_library_copy)
    health_mod.register_metrics_exporter(_product_leftover_copy)

    text = _scrape()

    for series in (
        'stapel_schema_probe_ok{service="iron_auth"} 1',
        'stapel_schema_at_head{service="iron_auth"} 1',
    ):
        assert text.count(series) == 1, text
    # Repeating HELP/TYPE for a metric is malformed exposition in its own right.
    assert text.count("# TYPE stapel_schema_probe_ok gauge") == 1
    assert text.count("# HELP stapel_schema_at_head") == 1


@override_settings(SERVICE_NAME="iron_auth")
def test_a_disagreement_is_logged_and_counted_not_silently_resolved(
    clean_registry, caplog,
):
    """The day the two copies disagree, which one wins must not be luck."""
    def live_probe():
        return 'stapel_schema_at_head{service="iron_auth"} 1'

    def cached_answer():
        return 'stapel_schema_at_head{service="iron_auth"} 0'

    health_mod.register_metrics_exporter(live_probe)
    health_mod.register_metrics_exporter(cached_answer)

    with caplog.at_level(logging.ERROR):
        text = _scrape()

    assert text.count('stapel_schema_at_head{service="iron_auth"}') == 1
    assert 'stapel_schema_at_head{service="iron_auth"} 1' in text
    assert 'stapel_metrics_series_conflicts{service="iron_auth"} 1' in text
    assert "live_probe" in caplog.text and "cached_answer" in caplog.text


@override_settings(SERVICE_NAME="iron_auth")
def test_a_clean_scrape_reports_no_conflicts(clean_registry):
    health_mod.register_metrics_exporter(_library_copy)
    assert 'stapel_metrics_series_conflicts{service="iron_auth"} 0' in _scrape()


# ---------------------------------------------------------------------------
# what dedup must NOT eat
# ---------------------------------------------------------------------------


@override_settings(SERVICE_NAME="iron_auth")
def test_the_same_metric_under_different_labels_survives(clean_registry):
    """A series identity is name AND labels — not the metric name."""
    health_mod.register_metrics_exporter(
        lambda: (
            '# HELP stapel_dependency_up Registered dependency reachability\n'
            '# TYPE stapel_dependency_up gauge\n'
            'stapel_dependency_up{service="a",dependency="livekit"} 1\n'
            'stapel_dependency_up{service="a",dependency="schema"} 0'
        )
    )
    text = _scrape()
    assert 'stapel_dependency_up{service="a",dependency="livekit"} 1' in text
    assert 'stapel_dependency_up{service="a",dependency="schema"} 0' in text


@override_settings(SERVICE_NAME="iron_auth")
def test_a_label_value_containing_a_space_is_not_split(clean_registry):
    health_mod.register_metrics_exporter(
        lambda: 'stapel_build_info{service="a",note="built on friday"} 1'
    )
    assert 'stapel_build_info{service="a",note="built on friday"} 1' in _scrape()


@override_settings(SERVICE_NAME="iron_auth")
def test_the_endpoints_own_series_are_still_there(clean_registry):
    text = _scrape()
    for name in ("stapel_up", "stapel_uptime_seconds", "stapel_service_info"):
        assert f'{name}{{service="iron_auth"' in text or f'{name}{{service="iron_auth"}}' in text


@override_settings(SERVICE_NAME="iron_auth", STAPEL_METRICS_PREFIX="iron_")
def test_the_conflict_gauge_honours_the_prefix(clean_registry):
    assert 'iron_metrics_series_conflicts{service="iron_auth"} 0' in _scrape()


# ---------------------------------------------------------------------------
# the registration seam covers only the easy half — but it covers it
# ---------------------------------------------------------------------------


def test_registering_the_same_callable_twice_is_a_no_op(clean_registry):
    health_mod.register_metrics_exporter(_library_copy)
    health_mod.register_metrics_exporter(_library_copy)
    assert health_mod._custom_metrics_exporters == [_library_copy]


@override_settings(SERVICE_NAME="iron_auth")
def test_a_failing_exporter_does_not_take_the_scrape_with_it(clean_registry, caplog):
    def boom():
        raise RuntimeError("collector is broken")

    health_mod.register_metrics_exporter(boom)
    health_mod.register_metrics_exporter(_library_copy)

    with caplog.at_level(logging.ERROR):
        text = _scrape()
    assert 'stapel_schema_probe_ok{service="iron_auth"} 1' in text
    assert "collector is broken" in caplog.text
