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
#: their documentation gives (MODULE.md / CHANGELOG).
DOCUMENTED_EXAMPLES = {
    WINDOW: ["60", "0", ""],
    # STAPEL_SITEMAP keys are read from the environment under their bare name.
    "CACHE_TTL": ["3600", ""],
    "PARTIAL_CACHE_TTL": ["300", ""],
    "PAGE_MAX_URLS": ["50000"],
    "PAGE_MAX_BYTES": ["52428800"],
    "BATCH_LIMIT": ["1000"],
    "MAX_URLS": ["1000000"],
    "CALL_TIMEOUT": ["5.0", "5"],
    "URL_TEMPLATE": ["{origin}{path}", "{origin}/{locale}{path}"],
    "ORIGIN": ["https://example.com", ""],
}


@pytest.mark.parametrize(
    "name, raw",
    [(n, v) for n, values in DOCUMENTED_EXAMPLES.items() for v in values],
)
def test_documented_examples_raise_no_scalar_check(monkeypatch, name, raw):
    from stapel_core.django import workspaces  # noqa: F401  registers its read
    from stapel_core.django.sitemap.conf import sitemap_settings

    monkeypatch.setenv(name, raw)
    found = [
        e for e in check_scalar_env_vars()
        if e.id in (E004_BOOL_ENV_VAR, W002_SCALAR_ENV_VAR) and name in e.msg
    ]
    assert found == []
    if name in sitemap_settings.defaults and raw.strip():
        default = sitemap_settings.defaults[name]
        assert sitemap_settings._raw(name) == type(default)(raw)


@pytest.mark.parametrize("name", [
    "CACHE_TTL", "PARTIAL_CACHE_TTL", "PAGE_MAX_URLS", "PAGE_MAX_BYTES",
    "BATCH_LIMIT", "MAX_URLS", "CALL_TIMEOUT",
])
def test_a_blank_sitemap_number_is_its_default(monkeypatch, name):
    from stapel_core.django.sitemap.conf import sitemap_settings

    monkeypatch.setenv(name, "")
    assert sitemap_settings._raw(name) == sitemap_settings.defaults[name]


def test_unprocessable_payload_documented_values_are_not_env_checked(settings):
    """co's posture key lives in the STAPEL_COMM dict; neither value trips a check."""
    from stapel_core.comm.config import comm_setting

    for value in ("park", "raise"):
        settings.STAPEL_COMM = {"UNPROCESSABLE_PAYLOAD": value}
        assert comm_setting("UNPROCESSABLE_PAYLOAD", "park") == value
        assert not [
            e for e in check_scalar_env_vars() if "UNPROCESSABLE_PAYLOAD" in e.msg
        ]
