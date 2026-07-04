"""Tests for stapel_core.netintel — provider seam, cache, fail-open, client_ip."""
import sys
import types
from types import SimpleNamespace
from unittest import mock

import pytest
from django.test import override_settings

import stapel_core.netintel as netintel
from stapel_core.netintel import (
    CACHE_KEY_PREFIX,
    HttpJsonProvider,
    IpKind,
    IpProfile,
    MaxMindProvider,
    NetIntelProvider,
    NullProvider,
    classify_ip,
    client_ip,
    country_of,
    unknown_profile,
)
from stapel_core.netintel.checks import (
    W001_UNIMPORTABLE,
    W002_NOT_A_PROVIDER,
    check_netintel_provider,
)
from stapel_core.netintel.providers import default_response_mapper


@pytest.fixture(autouse=True)
def _reset_warned():
    netintel._warned_providers.clear()
    yield
    netintel._warned_providers.clear()


class CountingProvider(NetIntelProvider):
    def __init__(self, profile=None):
        self.calls = 0
        self.profile = profile

    def classify(self, ip):
        self.calls += 1
        return self.profile or IpProfile(ip=ip, kind=IpKind.DATACENTER, asn=16509)


class RaisingProvider(NetIntelProvider):
    def classify(self, ip):
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


def test_unknown_profile_defaults():
    profile = unknown_profile("1.2.3.4")
    assert profile == IpProfile(ip="1.2.3.4", kind=IpKind.UNKNOWN)
    assert profile.asn is None and profile.country is None
    assert profile.confidence is None


def test_ip_kind_vocabulary():
    assert IpKind.ALL == ("residential", "datacenter", "vpn", "tor", "unknown")


# ---------------------------------------------------------------------------
# Default provider / public API
# ---------------------------------------------------------------------------


def test_default_is_null_provider_unknown():
    assert classify_ip("8.8.8.8").kind == IpKind.UNKNOWN
    assert country_of("8.8.8.8") is None


def test_empty_ip_is_unknown_without_provider_call():
    assert classify_ip(None).kind == IpKind.UNKNOWN
    assert classify_ip("").kind == IpKind.UNKNOWN


def test_dotted_path_provider_resolution():
    with override_settings(STAPEL_NETINTEL={
        "PROVIDER": "stapel_core.netintel.providers.NullProvider",
    }):
        assert isinstance(netintel._resolve_provider(), NullProvider)


def test_provider_instance_and_class_accepted():
    provider = CountingProvider()
    with override_settings(STAPEL_NETINTEL={"PROVIDER": provider}):
        assert netintel._resolve_provider() is provider
    with override_settings(STAPEL_NETINTEL={"PROVIDER": CountingProvider}):
        assert isinstance(netintel._resolve_provider(), CountingProvider)


def test_non_provider_value_fails_open(caplog):
    with override_settings(STAPEL_NETINTEL={"PROVIDER": object()}):
        with caplog.at_level("WARNING", logger="stapel_core.netintel"):
            assert classify_ip("9.9.9.9").kind == IpKind.UNKNOWN
    assert "failing open" in caplog.text


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_miss_then_hit():
    provider = CountingProvider()
    with override_settings(STAPEL_NETINTEL={"PROVIDER": provider}):
        first = classify_ip("203.0.113.7")
        second = classify_ip("203.0.113.7")
    assert provider.calls == 1
    assert first == second
    assert first.kind == IpKind.DATACENTER


def test_cache_key_prefix_and_ttl_default():
    provider = CountingProvider()
    fake_cache = mock.MagicMock()
    fake_cache.get.return_value = None
    with override_settings(STAPEL_NETINTEL={"PROVIDER": provider}):
        with mock.patch.object(netintel, "_cache", return_value=fake_cache):
            classify_ip("203.0.113.8")
    key, profile = fake_cache.set.call_args[0]
    assert key == CACHE_KEY_PREFIX + "203.0.113.8"
    assert profile.kind == IpKind.DATACENTER
    assert fake_cache.set.call_args[1]["timeout"] == 86400


def test_cache_ttl_setting_respected():
    provider = CountingProvider()
    fake_cache = mock.MagicMock()
    fake_cache.get.return_value = None
    with override_settings(STAPEL_NETINTEL={"PROVIDER": provider, "CACHE_TTL": 123}):
        with mock.patch.object(netintel, "_cache", return_value=fake_cache):
            classify_ip("203.0.113.9")
    assert fake_cache.set.call_args[1]["timeout"] == 123


def test_cache_alias_setting_respected():
    caches_config = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
        "netintel": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "netintel-tests",
        },
    }
    provider = CountingProvider()
    with override_settings(
        CACHES=caches_config,
        STAPEL_NETINTEL={"PROVIDER": provider, "CACHE_ALIAS": "netintel"},
    ):
        from django.core.cache import caches

        caches["netintel"].clear()
        classify_ip("198.51.100.4")
        assert caches["netintel"].get(CACHE_KEY_PREFIX + "198.51.100.4") is not None
        assert caches["default"].get(CACHE_KEY_PREFIX + "198.51.100.4") is None
        caches["netintel"].clear()


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


def test_raising_provider_fails_open_and_is_not_cached(caplog):
    with override_settings(STAPEL_NETINTEL={"PROVIDER": RaisingProvider}):
        with caplog.at_level("WARNING", logger="stapel_core.netintel"):
            assert classify_ip("203.0.113.20").kind == IpKind.UNKNOWN
        # not cached: a second call reaches the provider again (fails again)
        assert classify_ip("203.0.113.20").kind == IpKind.UNKNOWN
    from django.core.cache import cache

    assert cache.get(CACHE_KEY_PREFIX + "203.0.113.20") is None


def test_provider_failure_warns_once_per_class(caplog):
    with override_settings(STAPEL_NETINTEL={"PROVIDER": RaisingProvider}):
        with caplog.at_level("WARNING", logger="stapel_core.netintel"):
            classify_ip("203.0.113.21")
            classify_ip("203.0.113.22")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "RaisingProvider" in warnings[0].getMessage()


def test_classify_never_raises_on_cache_error():
    provider = CountingProvider()
    broken_cache = mock.MagicMock()
    broken_cache.get.side_effect = RuntimeError("cache down")
    with override_settings(STAPEL_NETINTEL={"PROVIDER": provider}):
        with mock.patch.object(netintel, "_cache", return_value=broken_cache):
            assert classify_ip("203.0.113.30").kind == IpKind.UNKNOWN


# ---------------------------------------------------------------------------
# MaxMindProvider — kind derivation matrix (geoip2 mocked)
# ---------------------------------------------------------------------------


class _AddressNotFound(Exception):
    pass


def _fake_geoip2(records):
    """Fake geoip2 modules; *records* maps db path → {method: record|None}."""

    class Reader:
        def __init__(self, path):
            self._data = records.get(path, {})

        def _lookup(self, method):
            record = self._data.get(method)
            if record is None:
                raise _AddressNotFound(method)
            return record

        def anonymous_ip(self, ip):
            return self._lookup("anonymous_ip")

        def asn(self, ip):
            return self._lookup("asn")

        def country(self, ip):
            return self._lookup("country")

    geoip2_mod = types.ModuleType("geoip2")
    database_mod = types.ModuleType("geoip2.database")
    errors_mod = types.ModuleType("geoip2.errors")
    database_mod.Reader = Reader
    errors_mod.AddressNotFoundError = _AddressNotFound
    geoip2_mod.database = database_mod
    geoip2_mod.errors = errors_mod
    return {
        "geoip2": geoip2_mod,
        "geoip2.database": database_mod,
        "geoip2.errors": errors_mod,
    }


def _maxmind_classify(records, **kwargs):
    provider = MaxMindProvider(
        asn_db="asn.mmdb", country_db="country.mmdb", anonymous_db="anon.mmdb",
        **kwargs,
    )
    with mock.patch.dict(sys.modules, _fake_geoip2(records)):
        return provider.classify("192.0.2.1")


def _anon(vpn=False, tor=False, hosting=False):
    return SimpleNamespace(
        is_anonymous_vpn=vpn, is_tor_exit_node=tor, is_hosting_provider=hosting,
    )


def _asn(number, org):
    return SimpleNamespace(
        autonomous_system_number=number, autonomous_system_organization=org,
    )


def _country(iso):
    return SimpleNamespace(country=SimpleNamespace(iso_code=iso))


def test_maxmind_vpn_flag():
    profile = _maxmind_classify({
        "anon.mmdb": {"anonymous_ip": _anon(vpn=True)},
        "asn.mmdb": {"asn": _asn(64512, "Some ISP")},
        "country.mmdb": {"country": _country("DE")},
    })
    assert profile.kind == IpKind.VPN
    assert profile.asn == 64512
    assert profile.country == "DE"


def test_maxmind_tor_flag_wins_over_vpn():
    profile = _maxmind_classify({
        "anon.mmdb": {"anonymous_ip": _anon(vpn=True, tor=True)},
    })
    assert profile.kind == IpKind.TOR


def test_maxmind_hosting_flag():
    profile = _maxmind_classify({
        "anon.mmdb": {"anonymous_ip": _anon(hosting=True)},
    })
    assert profile.kind == IpKind.DATACENTER


def test_maxmind_builtin_hosting_asn():
    profile = _maxmind_classify({
        "asn.mmdb": {"asn": _asn(16509, "Amazon.com, Inc.")},
    })
    assert profile.kind == IpKind.DATACENTER


def test_maxmind_extra_datacenter_asns_setting():
    records = {"asn.mmdb": {"asn": _asn(64999, "Tiny Telco")}}
    with override_settings(STAPEL_NETINTEL={"EXTRA_DATACENTER_ASNS": [64999]}):
        profile = _maxmind_classify(records)
    assert profile.kind == IpKind.DATACENTER


def test_maxmind_org_keyword_heuristic():
    profile = _maxmind_classify({
        "asn.mmdb": {"asn": _asn(64998, "Example Hosting GmbH")},
    })
    assert profile.kind == IpKind.DATACENTER


def test_maxmind_known_asn_is_residential():
    profile = _maxmind_classify({
        "asn.mmdb": {"asn": _asn(3320, "Deutsche Telekom AG")},
        "country.mmdb": {"country": _country("DE")},
    })
    assert profile.kind == IpKind.RESIDENTIAL
    assert profile.asn_org == "Deutsche Telekom AG"


def test_maxmind_nothing_found_is_unknown():
    profile = _maxmind_classify({})  # every lookup raises AddressNotFound
    assert profile.kind == IpKind.UNKNOWN
    assert profile.asn is None and profile.country is None


def test_maxmind_missing_geoip2_raises_clear_error():
    provider = MaxMindProvider(asn_db="asn.mmdb")
    with mock.patch.dict(sys.modules, {"geoip2": None, "geoip2.database": None,
                                       "geoip2.errors": None}):
        with pytest.raises(ImportError, match="netintel-maxmind"):
            provider.classify("192.0.2.1")


# ---------------------------------------------------------------------------
# HttpJsonProvider
# ---------------------------------------------------------------------------


def _custom_mapper(data, ip):
    return IpProfile(ip=ip, kind=IpKind.TOR, country=data.get("cc"))


def test_httpjson_requires_url_template():
    with pytest.raises(ValueError, match="HTTP_URL_TEMPLATE"):
        HttpJsonProvider().classify("192.0.2.1")


def test_httpjson_default_mapper_and_bearer_key():
    payload = {"country": "NL", "asn": "AS16276", "org": "OVH SAS", "vpn": True}
    with mock.patch("requests.get") as get:
        get.return_value.json.return_value = payload
        provider = HttpJsonProvider(
            url_template="https://api.example/{ip}", api_key="k3y",
        )
        profile = provider.classify("192.0.2.1")
    assert get.call_args[0][0] == "https://api.example/192.0.2.1"
    assert get.call_args[1]["headers"]["Authorization"] == "Bearer k3y"
    assert profile.kind == IpKind.VPN
    assert profile.asn == 16276
    assert profile.country == "NL"


def test_httpjson_mapper_seam_dotted_path():
    with override_settings(STAPEL_NETINTEL={
        "HTTP_URL_TEMPLATE": "https://api.example/{ip}",
        "HTTP_RESPONSE_MAPPER": "tests.test_netintel._custom_mapper",
    }):
        with mock.patch("requests.get") as get:
            get.return_value.json.return_value = {"cc": "SE"}
            with mock.patch(
                "django.utils.module_loading.import_string",
                return_value=_custom_mapper,
            ) as importer:
                profile = HttpJsonProvider().classify("192.0.2.2")
    importer.assert_called_once_with("tests.test_netintel._custom_mapper")
    assert profile.kind == IpKind.TOR
    assert profile.country == "SE"


def test_httpjson_mapper_seam_callable():
    provider = HttpJsonProvider(
        url_template="https://api.example/{ip}", response_mapper=_custom_mapper,
    )
    with mock.patch("requests.get") as get:
        get.return_value.json.return_value = {"cc": "FI"}
        profile = provider.classify("192.0.2.3")
    assert profile.kind == IpKind.TOR and profile.country == "FI"


def test_httpjson_http_error_bubbles_to_fail_open():
    with override_settings(STAPEL_NETINTEL={
        "PROVIDER": HttpJsonProvider(url_template="https://api.example/{ip}"),
    }):
        with mock.patch("requests.get", side_effect=ConnectionError("down")):
            assert classify_ip("192.0.2.4").kind == IpKind.UNKNOWN


def test_default_mapper_variants():
    assert default_response_mapper({"tor": True}, "x").kind == IpKind.TOR
    assert default_response_mapper({"proxy": True}, "x").kind == IpKind.VPN
    assert default_response_mapper({"hosting": True}, "x").kind == IpKind.DATACENTER
    assert default_response_mapper(
        {"connection_type": "Residential"}, "x"
    ).kind == IpKind.RESIDENTIAL
    assert default_response_mapper({}, "x").kind == IpKind.UNKNOWN
    # ipinfo-style org field carries both ASN and org name
    profile = default_response_mapper({"org": "AS15169 Google LLC"}, "x")
    assert profile.asn == 15169 and profile.asn_org == "Google LLC"
    # ipinfo-style nested asn object
    profile = default_response_mapper(
        {"asn": {"asn": "AS16509", "name": "Amazon"}}, "x"
    )
    assert profile.asn == 16509 and profile.asn_org == "Amazon"
    assert default_response_mapper({"country_code": "PL"}, "x").country == "PL"


# ---------------------------------------------------------------------------
# System checks
# ---------------------------------------------------------------------------


def test_check_passes_on_default_provider():
    assert check_netintel_provider() == []


def test_check_warns_on_unimportable_path():
    with override_settings(STAPEL_NETINTEL={"PROVIDER": "no.such.module.Provider"}):
        messages = check_netintel_provider()
    assert [m.id for m in messages] == [W001_UNIMPORTABLE]


def test_check_warns_on_non_provider():
    with override_settings(STAPEL_NETINTEL={
        "PROVIDER": "stapel_core.netintel.providers.logger",
    }):
        messages = check_netintel_provider()
    assert [m.id for m in messages] == [W002_NOT_A_PROVIDER]


def test_check_accepts_instance():
    with override_settings(STAPEL_NETINTEL={"PROVIDER": CountingProvider()}):
        assert check_netintel_provider() == []


# ---------------------------------------------------------------------------
# client_ip
# ---------------------------------------------------------------------------


class _Req:
    def __init__(self, meta):
        self.META = meta


def test_client_ip_defaults_to_remote_addr_only():
    request = _Req({
        "REMOTE_ADDR": "192.0.2.9",
        "HTTP_X_FORWARDED_FOR": "203.0.113.5",  # spoofable — ignored by default
    })
    assert client_ip(request) == "192.0.2.9"


def test_client_ip_none_request():
    assert client_ip(None) is None


def test_client_ip_trusted_proxy_header_first_hop():
    request = _Req({
        "REMOTE_ADDR": "10.0.0.1",
        "HTTP_X_FORWARDED_FOR": " 203.0.113.5 , 10.0.0.1",
    })
    with override_settings(STAPEL_NETINTEL={
        "TRUSTED_PROXY_HEADER": "HTTP_X_FORWARDED_FOR",
    }):
        assert client_ip(request) == "203.0.113.5"


def test_client_ip_trusted_header_absent_falls_back_to_remote_addr():
    request = _Req({"REMOTE_ADDR": "10.0.0.2"})
    with override_settings(STAPEL_NETINTEL={
        "TRUSTED_PROXY_HEADER": "HTTP_X_FORWARDED_FOR",
    }):
        assert client_ip(request) == "10.0.0.2"
