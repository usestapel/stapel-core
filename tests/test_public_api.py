"""Root package public API — lazy PEP 562 exports (stapel_core/__init__.py)."""
import pytest

import stapel_core

EXPECTED_ALL = sorted([
    "AbstractStapelUser",
    "AppSettings",
    "Event",
    "GDPRProvider",
    "JWTConfig",
    "JWTHandler",
    "StapelDataclassSerializer",
    "StapelErrorResponse",
    "StapelResponse",
    "TokenBlacklist",
    "TokenManager",
    "__version__",
    "Flow",
    "VerificationFactor",
    "call",
    "emit",
    "flow_registry",
    "flow_step",
    "function",
    "gdpr_registry",
    "register_factor",
    "requires_verification",
    "get_bus",
    "on_action",
    "publish",
    "signals",
    "start",
    "status",
    "task_handler",
])


def test_all_is_sorted():
    assert stapel_core.__all__ == sorted(stapel_core.__all__)


def test_all_matches_expected_names():
    assert stapel_core.__all__ == EXPECTED_ALL


@pytest.mark.parametrize("name", EXPECTED_ALL)
def test_every_export_resolves(name):
    assert getattr(stapel_core, name) is not None


def test_exports_point_at_canonical_objects():
    from stapel_core.bus import Event, get_bus, publish
    from stapel_core.comm import call, emit, function, on_action, start, status, task_handler
    from stapel_core.conf import AppSettings
    from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
    from stapel_core.django.api.serializers import StapelDataclassSerializer
    from stapel_core.django.users.models import AbstractStapelUser
    from stapel_core.gdpr import GDPRProvider, gdpr_registry

    assert stapel_core.emit is emit
    assert stapel_core.on_action is on_action
    assert stapel_core.call is call
    assert stapel_core.function is function
    assert stapel_core.start is start
    assert stapel_core.status is status
    assert stapel_core.task_handler is task_handler
    assert stapel_core.publish is publish
    assert stapel_core.get_bus is get_bus
    assert stapel_core.Event is Event
    assert stapel_core.AppSettings is AppSettings
    assert stapel_core.StapelResponse is StapelResponse
    assert stapel_core.StapelErrorResponse is StapelErrorResponse
    assert stapel_core.StapelDataclassSerializer is StapelDataclassSerializer
    assert stapel_core.GDPRProvider is GDPRProvider
    assert stapel_core.gdpr_registry is gdpr_registry
    assert stapel_core.AbstractStapelUser is AbstractStapelUser


def test_signals_module_export():
    import stapel_core.signals as signals_mod

    assert stapel_core.signals is signals_mod
    assert hasattr(stapel_core.signals, "user_registered")
    assert hasattr(stapel_core.signals, "payment_completed")


def test_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError, match="has no attribute 'does_not_exist'"):
        stapel_core.does_not_exist


def test_dir_includes_all_lazy_names():
    listing = dir(stapel_core)
    for name in EXPECTED_ALL:
        assert name in listing


def test_lazy_attribute_is_cached_in_module_globals():
    first = stapel_core.AppSettings
    assert vars(stapel_core)["AppSettings"] is first
    assert stapel_core.AppSettings is first
