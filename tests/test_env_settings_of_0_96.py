"""The 0.96.0 environment rules hold for every setting 0.96.0 introduced.

A blank value is "unset" (stapel_core.conf.E004's release). The settings other
changes of the same release added must follow it: none parses its environment
value by hand, a blank one means the default, and their documented example
values raise neither E004 nor W002.
"""
import os
import subprocess
import sys
import textwrap

import pytest

from stapel_core.conf import env_number, rejected_env_numbers
from stapel_core.django.sitemap import conf as sitemap_conf
from stapel_core.conf_checks import (
    E004_BOOL_ENV_VAR,
    W002_SCALAR_ENV_VAR,
    check_scalar_env_vars,
)

WINDOW = "WORKSPACES_NO_VERDICT_LOG_WINDOW_SECONDS"


class TestEnvNumber:
    @pytest.mark.parametrize("raw", ["", " ", "\t"])
    def test_blank_is_the_default(self, monkeypatch, raw):
        monkeypatch.setenv("TESTENVNUM_WINDOW", raw)
        assert env_number("TESTENVNUM_WINDOW", 60.0) == 60.0

    def test_unset_is_the_default(self, monkeypatch):
        monkeypatch.delenv("TESTENVNUM_WINDOW", raising=False)
        assert env_number("TESTENVNUM_WINDOW", 60.0) == 60.0

    def test_a_number_is_read_in_the_defaults_type(self, monkeypatch):
        monkeypatch.setenv("TESTENVNUM_WINDOW", " 0 ")
        assert env_number("TESTENVNUM_WINDOW", 60.0) == 0.0
        monkeypatch.setenv("TESTENVNUM_COUNT", "7")
        value = env_number("TESTENVNUM_COUNT", 3)
        assert value == 7 and type(value) is int

    def test_garbage_is_the_default_and_a_warning(self, monkeypatch):
        monkeypatch.setenv("TESTENVNUM_WINDOW", "sixty")
        assert env_number("TESTENVNUM_WINDOW", 60.0) == 60.0
        assert ("TESTENVNUM_WINDOW", "sixty", 60.0) in rejected_env_numbers()
        found = [e for e in check_scalar_env_vars() if "TESTENVNUM_WINDOW" in e.msg]
        assert [e.id for e in found] == [W002_SCALAR_ENV_VAR]

    def test_blank_is_never_reported(self, monkeypatch):
        monkeypatch.setenv("TESTENVNUM_WINDOW", "")
        env_number("TESTENVNUM_WINDOW", 60.0)
        assert not [e for e in check_scalar_env_vars() if "TESTENVNUM_WINDOW" in e.msg]

    def test_a_bool_default_is_refused(self):
        with pytest.raises(TypeError):
            env_number("TESTENVNUM_FLAG", True)


def _window_in_a_fresh_process(raw):
    env = {k: v for k, v in os.environ.items() if k != WINDOW}
    if raw is not None:
        env[WINDOW] = raw
    script = textwrap.dedent("""
        from stapel_core.testing import configure_django
        configure_django(installed_apps=[], extra_settings={"CACHES": {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}})
        from stapel_core.django import workspaces
        print(repr(workspaces.NO_VERDICT_LOG_WINDOW_SECONDS))
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        env=env, cwd="/",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("raw, expected", [
    (None, "60.0"), ("", "60.0"), ("  ", "60.0"), ("garbage", "60.0"),
    ("0", "0.0"), ("15", "15.0"),
])
def test_workspaces_log_window_follows_the_blank_is_unset_rule(raw, expected):
    """A blank or garbage window used to raise ValueError at import."""
    assert _window_in_a_fresh_process(raw) == expected


def test_workspaces_log_window_is_not_parsed_by_hand():
    from stapel_core.django import workspaces

    source = open(workspaces.__file__, encoding="utf-8").read()
    assert f'env_number(\n    "{WINDOW}"' in source
    assert f'os.getenv("{WINDOW}"' not in source


#: Every setting the other 0.96.0 changes introduced, with the example values
#: their documentation gives (MODULE.md / CHANGELOG / CONFIG.MD).
DOCUMENTED_EXAMPLES = {
    WINDOW: ["60", "0", ""],
    # STAPEL_SITEMAP keys are read from the environment as STAPEL_SITEMAP_<KEY>.
    "STAPEL_SITEMAP_CACHE_TTL": ["3600", ""],
    "STAPEL_SITEMAP_PARTIAL_CACHE_TTL": ["300", ""],
    "STAPEL_SITEMAP_PAGE_MAX_URLS": ["50000"],
    "STAPEL_SITEMAP_PAGE_MAX_BYTES": ["52428800"],
    "STAPEL_SITEMAP_BATCH_LIMIT": ["1000"],
    "STAPEL_SITEMAP_MAX_URLS": ["1000000"],
    "STAPEL_SITEMAP_CALL_TIMEOUT": ["5.0", "5"],
    "STAPEL_SITEMAP_URL_TEMPLATE": ["{origin}{path}", "{origin}/{locale}{path}"],
    "STAPEL_SITEMAP_ORIGIN": ["https://example.com", ""],
}

SITEMAP_NUMBERS = [
    "CACHE_TTL", "PARTIAL_CACHE_TTL", "PAGE_MAX_URLS", "PAGE_MAX_BYTES",
    "BATCH_LIMIT", "MAX_URLS", "CALL_TIMEOUT",
]


@pytest.fixture
def sitemap_settings():
    from stapel_core.django.sitemap.conf import sitemap_settings

    sitemap_settings.reload()
    yield sitemap_settings
    sitemap_settings.reload()


@pytest.mark.parametrize(
    "name, raw",
    [(n, v) for n, values in DOCUMENTED_EXAMPLES.items() for v in values],
)
def test_documented_examples_raise_no_scalar_check(monkeypatch, sitemap_settings, name, raw):
    from stapel_core.django import workspaces  # noqa: F401  registers its read
    from stapel_core.django.sitemap.conf import ENV_PREFIX

    monkeypatch.setenv(name, raw)
    found = [
        e for e in check_scalar_env_vars()
        if e.id in (E004_BOOL_ENV_VAR, W002_SCALAR_ENV_VAR) and name in e.msg
    ]
    assert found == []
    key = name.removeprefix(ENV_PREFIX)
    if name.startswith(ENV_PREFIX) and raw.strip():
        default = sitemap_settings.defaults[key]
        assert getattr(sitemap_settings, key) == type(default)(raw)


@pytest.mark.parametrize("key", SITEMAP_NUMBERS)
def test_a_blank_sitemap_number_is_its_default(monkeypatch, sitemap_settings, key):
    monkeypatch.setenv(f"STAPEL_SITEMAP_{key}", "")
    assert getattr(sitemap_settings, key) == sitemap_settings.defaults[key]


@pytest.mark.parametrize("key", list(sitemap_conf.sitemap_settings.defaults))
def test_a_bare_name_never_reaches_the_sitemap(monkeypatch, sitemap_settings, key):
    """Every key: the bare environment name is not read at all."""
    default = sitemap_settings.defaults[key]
    monkeypatch.setenv(key, "https://stray.example" if isinstance(default, str) else "7")
    assert sitemap_settings.env_var_names(key) == (f"STAPEL_SITEMAP_{key}",)
    assert getattr(sitemap_settings, key) == default


def test_bare_origin_and_cache_ttl_do_not_change_the_built_urls(monkeypatch, sitemap_settings):
    monkeypatch.setenv("ORIGIN", "https://stray.example")
    monkeypatch.setenv("CACHE_TTL", "1")
    assert sitemap_settings.ORIGIN == ""
    assert sitemap_settings.CACHE_TTL == 3600


def test_prefixed_names_do_reach_the_sitemap(monkeypatch, sitemap_settings):
    monkeypatch.setenv("STAPEL_SITEMAP_ORIGIN", "https://shop.example")
    monkeypatch.setenv("STAPEL_SITEMAP_CACHE_TTL", "120")
    monkeypatch.setenv("STAPEL_SITEMAP_CALL_TIMEOUT", "2.5")
    assert sitemap_settings.ORIGIN == "https://shop.example"
    assert sitemap_settings.CACHE_TTL == 120
    assert sitemap_settings.CALL_TIMEOUT == 2.5


def test_a_prefixed_garbage_number_is_named_by_w002(monkeypatch, sitemap_settings):
    monkeypatch.setenv("STAPEL_SITEMAP_CACHE_TTL", "an hour")
    found = [e for e in check_scalar_env_vars() if "STAPEL_SITEMAP_CACHE_TTL" in e.msg]
    assert [e.id for e in found] == [W002_SCALAR_ENV_VAR]


def test_providers_in_the_environment_is_refused_only_under_its_prefixed_name(
    monkeypatch, sitemap_settings
):
    from django.core.exceptions import ImproperlyConfigured

    monkeypatch.setenv("PROVIDERS", "cms.sitemap_entries")
    assert sitemap_settings.PROVIDERS == {}
    sitemap_settings.reload()
    monkeypatch.setenv("STAPEL_SITEMAP_PROVIDERS", "cms.sitemap_entries")
    with pytest.raises(ImproperlyConfigured, match="STAPEL_SITEMAP_PROVIDERS"):
        sitemap_settings.PROVIDERS


def test_unprocessable_payload_documented_values_are_not_env_checked(settings):
    """co's posture key lives in the STAPEL_COMM dict; neither value trips a check."""
    from stapel_core.comm.config import comm_setting

    for value in ("park", "raise"):
        settings.STAPEL_COMM = {"UNPROCESSABLE_PAYLOAD": value}
        assert comm_setting("UNPROCESSABLE_PAYLOAD", "park") == value
        assert not [
            e for e in check_scalar_env_vars() if "UNPROCESSABLE_PAYLOAD" in e.msg
        ]
