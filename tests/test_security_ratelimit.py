"""``parse_rate`` in the kernel (``stapel_core.security.ratelimit``).

The gateway keeps its name and its error contract: a malformed rate read
through ``stapel_core.gateway.ratelimit.parse_rate`` is still a
``GatewayConfigError`` with the same message.
"""
import pytest

from stapel_core.security import InvalidRate, parse_rate
from stapel_core.security.ratelimit import parse_rate as impl


@pytest.mark.parametrize("rate,expected", [
    ("30/m", (30, 60)),
    ("5/s", (5, 1)),
    ("100/h", (100, 3600)),
    ("500/d", (500, 86400)),
    ("10/900", (10, 900)),
    (" 2 / h ", (2, 3600)),
])
def test_forms(rate, expected):
    assert parse_rate(rate) == expected


@pytest.mark.parametrize("bad", [None, "", "fast", "0/m", "10/0", "10/y", "-1/m"])
def test_malformed_rate_is_an_error_never_unlimited(bad):
    with pytest.raises(InvalidRate) as info:
        parse_rate(bad)
    assert isinstance(info.value, ValueError)
    assert info.value.rate == bad


def test_package_export_is_the_module_function():
    assert parse_rate is impl


@pytest.mark.parametrize("rate,message", [
    ("fast", "rate_limit 'fast' is malformed (expected 'N/s|m|h|d' or 'N/SECONDS')"),
    ("0/m", "rate_limit '0/m' must be positive"),
])
def test_old_path_keeps_the_gateway_error_contract(rate, message):
    from stapel_core.gateway.exceptions import GatewayConfigError
    from stapel_core.gateway.ratelimit import parse_rate as old

    assert old("30/m") == (30, 60)
    with pytest.raises(GatewayConfigError) as info:
        old(rate)
    assert str(info.value) == message
    assert info.value.reason == "gateway_misconfigured"
