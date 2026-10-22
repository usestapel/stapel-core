"""An env var for a scalar setting arrived as a raw string.

``MAX_IMAGE_SIZE=5242880`` in the environment made ``size > cap`` compare an
int with a str (TypeError, a 500 on every upload) and ``REQUIRE_DECODER=False``
stayed truthy. A key whose declared default is a bool, int or float is now
read back in that type; a value that does not parse passes through as the
string it is and ``manage.py check`` names it (W002). Keys a library parses
itself (a ceiling that takes "unlimited") are declared ``env_raw``.
"""
import pytest

from stapel_core.conf import AppSettings
from stapel_core.conf_checks import W002_SCALAR_ENV_VAR, check_scalar_env_vars


@pytest.fixture
def cdn_like():
    return AppSettings(
        "STAPEL_TESTSCALAR",
        defaults={
            "MAX_IMAGE_SIZE": 10 * 1024 * 1024,
            "REQUIRE_DECODER": True,
            "QUALITY_RATIO": 0.8,
            "BUCKET": "media",
            "OPTIONAL_LIMIT": None,
            "MAX_OBJECTS": 1000,
        },
        env_raw=("MAX_OBJECTS",),
    )


def test_an_int_key_from_env_is_an_int(cdn_like, monkeypatch):
    monkeypatch.setenv("MAX_IMAGE_SIZE", "5242880")
    value = cdn_like.MAX_IMAGE_SIZE
    assert value == 5242880
    assert isinstance(value, int)
    assert (6_000_000 > value) is True


@pytest.mark.parametrize("raw,expected", [
    ("False", False), ("false", False), ("0", False), ("no", False), ("off", False),
    ("True", True), ("1", True), ("yes", True), (" on ", True),
])
def test_a_bool_key_from_env_is_a_bool(cdn_like, monkeypatch, raw, expected):
    monkeypatch.setenv("REQUIRE_DECODER", raw)
    assert cdn_like.REQUIRE_DECODER is expected


def test_a_float_key_from_env_is_a_float(cdn_like, monkeypatch):
    monkeypatch.setenv("QUALITY_RATIO", "0.5")
    assert cdn_like.QUALITY_RATIO == 0.5


def test_string_and_none_defaults_stay_strings(cdn_like, monkeypatch):
    monkeypatch.setenv("BUCKET", "42")
    monkeypatch.setenv("OPTIONAL_LIMIT", "7")
    assert cdn_like.BUCKET == "42"
    assert cdn_like.OPTIONAL_LIMIT == "7"


def test_an_unparseable_value_passes_through_as_the_string(cdn_like, monkeypatch):
    monkeypatch.setenv("MAX_IMAGE_SIZE", "5MB")
    assert cdn_like.MAX_IMAGE_SIZE == "5MB"


def test_an_env_raw_key_is_never_coerced(cdn_like, monkeypatch):
    monkeypatch.setenv("MAX_OBJECTS", "unlimited")
    assert cdn_like.MAX_OBJECTS == "unlimited"
    monkeypatch.setenv("MAX_OBJECTS", "50")
    cdn_like.reload()
    assert cdn_like.MAX_OBJECTS == "50"
    assert not [e for e in check_scalar_env_vars() if "MAX_OBJECTS" in e.msg]


def test_env_raw_must_name_a_declared_key():
    with pytest.raises(ValueError):
        AppSettings("STAPEL_TESTSCALAR2", defaults={"A": 1}, env_raw=("B",))


def test_settings_values_are_not_touched(cdn_like, settings):
    settings.STAPEL_TESTSCALAR = {"MAX_IMAGE_SIZE": "kept-as-written"}
    cdn_like.reload()
    assert cdn_like.MAX_IMAGE_SIZE == "kept-as-written"


def test_boot_check_names_the_bad_variable(cdn_like, monkeypatch):
    monkeypatch.setenv("MAX_IMAGE_SIZE", "lots")
    errors = [e for e in check_scalar_env_vars() if "MAX_IMAGE_SIZE" in e.msg]
    assert [e.id for e in errors] == [W002_SCALAR_ENV_VAR]
    assert "int" in errors[0].msg


def test_boot_check_is_silent_on_good_values(cdn_like, monkeypatch):
    monkeypatch.setenv("MAX_IMAGE_SIZE", "100")
    monkeypatch.setenv("REQUIRE_DECODER", "false")
    assert not [e for e in check_scalar_env_vars() if "STAPEL_TESTSCALAR" in e.msg]
