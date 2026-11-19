"""The client-IP seam in ``stapel_core.security``.

``client_ip()`` and its ``TRUSTED_PROXY_HEADER`` moved from netintel to the
security namespace. The trust semantics must not change by one bit for any
configuration that existed before the move, so the table below compares the
new resolution against the 0.97.0 rule, written out independently:
``STAPEL_NETINTEL`` dict → flat ``TRUSTED_PROXY_HEADER`` setting → ``None``,
never the environment.
"""
from __future__ import annotations

import itertools

import pytest
from django.test import override_settings

import stapel_core.netintel as netintel
from stapel_core import security
from stapel_core.netintel.checks import W003_NOT_CONFIGURED, check_netintel_provider
from stapel_core.netintel.conf import netintel_settings
from stapel_core.security.checks import (
    W001_LEGACY_PROXY_HEADER_KEY,
    W002_PROXY_HEADER_KEYS_DISAGREE,
    check_trusted_proxy_header_key,
)
from stapel_core.security.conf import security_settings

_UNSET = object()


class _Req:
    def __init__(self, meta):
        self.META = meta


def _settings_kwargs(new, old, flat):
    kwargs = {}
    if new is not _UNSET:
        kwargs["STAPEL_SECURITY"] = {"TRUSTED_PROXY_HEADER": new}
    if old is not _UNSET:
        kwargs["STAPEL_NETINTEL"] = {"TRUSTED_PROXY_HEADER": old}
    if flat is not _UNSET:
        kwargs["TRUSTED_PROXY_HEADER"] = flat
    return kwargs


def _rule_of_0_97(old, flat):
    """The header 0.97.0 trusted: STAPEL_NETINTEL dict, then flat, then None."""
    if old is not _UNSET:
        return old
    if flat is not _UNSET:
        return flat
    return None


def _expected(new, old, flat):
    return new if new is not _UNSET else _rule_of_0_97(old, flat)


def _first_hop_or_remote(meta, header):
    if header:
        for candidate in str(meta.get(header, "")).split(","):
            if candidate.strip():
                return candidate.strip()
    return meta.get("REMOTE_ADDR") or None


_VALUES = (_UNSET, None, "", "HTTP_X_REAL_IP", "HTTP_X_FORWARDED_FOR")
_REQUESTS = (
    {"REMOTE_ADDR": "10.0.0.1"},
    {"REMOTE_ADDR": "10.0.0.1", "HTTP_X_REAL_IP": "198.51.100.7"},
    {"REMOTE_ADDR": "10.0.0.1", "HTTP_X_FORWARDED_FOR": " 203.0.113.5 , 10.0.0.1"},
    {
        "REMOTE_ADDR": "10.0.0.1",
        "HTTP_X_REAL_IP": "198.51.100.7",
        "HTTP_X_FORWARDED_FOR": "203.0.113.5",
    },
    {"HTTP_X_FORWARDED_FOR": " , "},
    {},
)


def _label(value):
    return "unset" if value is _UNSET else repr(value)


@pytest.mark.parametrize(
    "new,old,flat",
    list(itertools.product(_VALUES, repeat=3)),
    ids=lambda v: _label(v),
)
def test_resolution_table(new, old, flat):
    """Old key vs new key vs both (new wins) vs none, with and without flat."""
    with override_settings(**_settings_kwargs(new, old, flat)):
        assert security.trusted_proxy_header() == _expected(new, old, flat)
        assert netintel_settings.TRUSTED_PROXY_HEADER == _expected(new, old, flat)
        for meta in _REQUESTS:
            want = _first_hop_or_remote(meta, _expected(new, old, flat))
            assert security.client_ip(_Req(meta)) == want
            assert netintel.client_ip(_Req(meta)) == want


@pytest.mark.parametrize(
    "old,flat",
    [(o, f) for o, f in itertools.product(_VALUES, repeat=2)],
    ids=lambda v: _label(v),
)
def test_every_pre_move_configuration_trusts_exactly_what_it_did(old, flat):
    """Without the new key, the answer is the 0.97.0 answer — bit for bit."""
    with override_settings(**_settings_kwargs(_UNSET, old, flat)):
        for meta in _REQUESTS:
            assert netintel.client_ip(_Req(meta)) == _first_hop_or_remote(
                meta, _rule_of_0_97(old, flat)
            )


def test_old_import_path_is_the_same_function():
    from stapel_core.netintel import client_ip as old
    from stapel_core.security import client_ip as new
    from stapel_core.security.proxy import client_ip as impl

    assert old is new is impl


def test_none_request():
    assert security.client_ip(None) is None


def test_environment_never_sets_the_header(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HEADER", "HTTP_X_FORWARDED_FOR")
    security_settings.reload()
    netintel_settings.reload()
    try:
        assert security_settings.TRUSTED_PROXY_HEADER is None
        assert security.trusted_proxy_header() is None
        request = _Req({"REMOTE_ADDR": "10.0.0.1", "HTTP_X_FORWARDED_FOR": "6.6.6.6"})
        assert security.client_ip(request) == "10.0.0.1"
        assert "TRUSTED_PROXY_HEADER" in security_settings.env_closed_keys()
    finally:
        security_settings.reload()
        netintel_settings.reload()


# --- netintel.W003 no longer reads the proxy header as classification intent


def test_w003_silent_when_only_the_legacy_proxy_header_is_set():
    with override_settings(STAPEL_NETINTEL={"TRUSTED_PROXY_HEADER": "HTTP_X_REAL_IP"}):
        assert check_netintel_provider() == []


def test_w003_silent_when_only_the_new_proxy_header_is_set():
    with override_settings(STAPEL_SECURITY={"TRUSTED_PROXY_HEADER": "HTTP_X_REAL_IP"}):
        assert check_netintel_provider() == []


def test_w003_still_fires_for_classification_intent_next_to_the_header():
    with override_settings(STAPEL_NETINTEL={
        "TRUSTED_PROXY_HEADER": "HTTP_X_REAL_IP",
        "MAXMIND_ASN_DB": "/srv/geo/asn.mmdb",
    }):
        messages = check_netintel_provider()
    assert [m.id for m in messages] == [W003_NOT_CONFIGURED]
    assert "MAXMIND_ASN_DB" in messages[0].msg
    assert "TRUSTED_PROXY_HEADER" not in messages[0].msg


def test_w003_inversion_the_old_intent_list_fires(monkeypatch):
    """The silent case above is silent because of the intent list, not by luck."""
    from stapel_core.netintel import checks as netintel_checks

    monkeypatch.setattr(
        netintel_checks, "_INTENT_KEYS",
        (*netintel_checks._INTENT_KEYS, "TRUSTED_PROXY_HEADER"),
    )
    with override_settings(STAPEL_NETINTEL={"TRUSTED_PROXY_HEADER": "HTTP_X_REAL_IP"}):
        assert [m.id for m in check_netintel_provider()] == [W003_NOT_CONFIGURED]


# --- deprecation of the legacy key -------------------------------------------


def test_no_finding_without_the_legacy_key():
    assert check_trusted_proxy_header_key() == []
    with override_settings(STAPEL_SECURITY={"TRUSTED_PROXY_HEADER": "HTTP_X_REAL_IP"}):
        assert check_trusted_proxy_header_key() == []
    with override_settings(TRUSTED_PROXY_HEADER="HTTP_X_REAL_IP"):
        assert check_trusted_proxy_header_key() == []


def test_legacy_key_is_a_deprecation_warning_not_an_error():
    from django.core import checks

    with override_settings(STAPEL_NETINTEL={"TRUSTED_PROXY_HEADER": "HTTP_X_REAL_IP"}):
        messages = check_trusted_proxy_header_key()
    assert [m.id for m in messages] == [W001_LEGACY_PROXY_HEADER_KEY]
    assert messages[0].level == checks.WARNING
    assert "STAPEL_SECURITY['TRUSTED_PROXY_HEADER']" in messages[0].msg


def test_legacy_key_equal_to_the_new_one_is_still_deprecated():
    with override_settings(
        STAPEL_NETINTEL={"TRUSTED_PROXY_HEADER": "HTTP_X_REAL_IP"},
        STAPEL_SECURITY={"TRUSTED_PROXY_HEADER": "HTTP_X_REAL_IP"},
    ):
        assert [m.id for m in check_trusted_proxy_header_key()] == [
            W001_LEGACY_PROXY_HEADER_KEY
        ]


def test_disagreeing_keys_name_the_winner():
    with override_settings(
        STAPEL_NETINTEL={"TRUSTED_PROXY_HEADER": "HTTP_X_FORWARDED_FOR"},
        STAPEL_SECURITY={"TRUSTED_PROXY_HEADER": "HTTP_X_REAL_IP"},
    ):
        messages = check_trusted_proxy_header_key()
        assert security.trusted_proxy_header() == "HTTP_X_REAL_IP"
    assert [m.id for m in messages] == [W002_PROXY_HEADER_KEYS_DISAGREE]


def test_the_deprecation_check_is_registered():
    from django.core.checks.registry import registry

    assert check_trusted_proxy_header_key in registry.get_checks()
