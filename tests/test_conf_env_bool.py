"""A blank environment value is unset; an unrecognised boolean is refused.

``KEY=`` is what an env-file template leaves behind. Reading it as False opens
every gate that defaults to on, so a blank value means "unset" and the
declared default applies. A word that is not a boolean resolves to the
declared default (the value the library ships as safe) and ``manage.py
check`` refuses it with ``stapel_core.conf.E004``.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from stapel_core import conf as conf_module
from stapel_core.conf import AppSettings, env_bool, parse_bool_word
from stapel_core.conf_checks import (
    E004_BOOL_ENV_VAR,
    W002_SCALAR_ENV_VAR,
    check_scalar_env_vars,
)

TRUE_WORDS = ["1", "true", "TRUE", "True", " yes ", "on", "ON"]
FALSE_WORDS = ["0", "false", "FALSE", "False", " no ", "off", "Off"]
BLANKS = ["", " ", "\t", "  \n"]
GARBAGE = ["maybe", "disabled", "enabled", "nope", "2", "t", "f"]


@pytest.fixture
def gates():
    return AppSettings(
        "STAPEL_TESTENVBOOL",
        defaults={
            "TESTENVBOOL_GATE": True,
            "TESTENVBOOL_HATCH": False,
            "TESTENVBOOL_LIMIT": 10,
            "TESTENVBOOL_LABEL": "x",
        },
    )


def _read(app_settings, key):
    app_settings.reload()
    return getattr(app_settings, key)


class TestAppSettingsBool:
    @pytest.mark.parametrize("raw", BLANKS)
    def test_a_blank_value_keeps_a_default_on_gate_on(self, gates, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_GATE", raw)
        assert _read(gates, "TESTENVBOOL_GATE") is True

    @pytest.mark.parametrize("raw", BLANKS)
    def test_a_blank_value_keeps_a_default_off_hatch_shut(self, gates, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_HATCH", raw)
        assert _read(gates, "TESTENVBOOL_HATCH") is False

    @pytest.mark.parametrize("raw", GARBAGE)
    def test_an_unrecognised_word_keeps_each_default(self, gates, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_GATE", raw)
        monkeypatch.setenv("TESTENVBOOL_HATCH", raw)
        assert _read(gates, "TESTENVBOOL_GATE") is True
        assert _read(gates, "TESTENVBOOL_HATCH") is False

    @pytest.mark.parametrize("raw", FALSE_WORDS)
    def test_only_a_false_word_turns_the_gate_off(self, gates, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_GATE", raw)
        assert _read(gates, "TESTENVBOOL_GATE") is False

    @pytest.mark.parametrize("raw", TRUE_WORDS)
    def test_only_a_true_word_opens_the_hatch(self, gates, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_HATCH", raw)
        assert _read(gates, "TESTENVBOOL_HATCH") is True

    def test_a_blank_int_is_the_default(self, gates, monkeypatch):
        monkeypatch.setenv("TESTENVBOOL_LIMIT", "")
        assert _read(gates, "TESTENVBOOL_LIMIT") == 10

    def test_a_blank_string_key_stays_blank(self, gates, monkeypatch):
        # Strings carry their own meaning for "empty"; only scalars are unset.
        monkeypatch.setenv("TESTENVBOOL_LABEL", "")
        assert _read(gates, "TESTENVBOOL_LABEL") == ""


class TestBootCheck:
    def _ours(self):
        return [e for e in check_scalar_env_vars() if "TESTENVBOOL" in e.msg]

    @pytest.mark.parametrize("raw", GARBAGE)
    def test_an_unrecognised_bool_is_an_error(self, gates, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_GATE", raw)
        found = self._ours()
        assert [e.id for e in found] == [E004_BOOL_ENV_VAR]
        assert found[0].is_serious()
        assert "TESTENVBOOL_GATE" in found[0].msg

    @pytest.mark.parametrize("raw", BLANKS + TRUE_WORDS + FALSE_WORDS)
    def test_blank_and_boolean_words_are_silent(self, gates, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_GATE", raw)
        monkeypatch.setenv("TESTENVBOOL_HATCH", raw)
        assert self._ours() == []

    def test_an_unparseable_int_stays_a_warning(self, gates, monkeypatch):
        monkeypatch.setenv("TESTENVBOOL_LIMIT", "lots")
        assert [e.id for e in self._ours()] == [W002_SCALAR_ENV_VAR]


class TestEnvBool:
    @pytest.fixture(autouse=True)
    def _isolated_registry(self, monkeypatch):
        monkeypatch.setattr(conf_module, "_ENV_BOOL_READS", {})

    @pytest.mark.parametrize("raw", BLANKS + GARBAGE)
    def test_blank_or_garbage_keeps_the_default(self, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_FLAG", raw)
        assert env_bool("TESTENVBOOL_FLAG", True) is True
        assert env_bool("TESTENVBOOL_FLAG", False) is False

    def test_unset_is_the_default(self, monkeypatch):
        monkeypatch.delenv("TESTENVBOOL_FLAG", raising=False)
        assert env_bool("TESTENVBOOL_FLAG", True) is True
        assert env_bool("TESTENVBOOL_FLAG", None) is None

    @pytest.mark.parametrize("raw", FALSE_WORDS)
    def test_false_words(self, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_FLAG", raw)
        assert env_bool("TESTENVBOOL_FLAG", True) is False

    @pytest.mark.parametrize("raw", TRUE_WORDS)
    def test_true_words(self, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_FLAG", raw)
        assert env_bool("TESTENVBOOL_FLAG", False) is True

    @pytest.mark.parametrize("raw", BLANKS + GARBAGE)
    def test_unanswered_overrides_the_default_only_when_set(self, monkeypatch, raw):
        monkeypatch.setenv("TESTENVBOOL_FLAG", raw)
        assert env_bool("TESTENVBOOL_FLAG", None, unanswered=False) is False
        monkeypatch.delenv("TESTENVBOOL_FLAG")
        assert env_bool("TESTENVBOOL_FLAG", None, unanswered=False) is None

    def test_the_boot_check_sees_reads_outside_any_namespace(self, monkeypatch):
        monkeypatch.setenv("TESTENVBOOL_FLAG", "maybe")
        env_bool("TESTENVBOOL_FLAG", True)
        found = [e for e in check_scalar_env_vars() if "TESTENVBOOL_FLAG" in e.msg]
        assert [e.id for e in found] == [E004_BOOL_ENV_VAR]
        monkeypatch.setenv("TESTENVBOOL_FLAG", "")
        assert not [e for e in check_scalar_env_vars() if "TESTENVBOOL_FLAG" in e.msg]

    def test_parse_bool_word_refuses_garbage(self):
        with pytest.raises(ValueError):
            parse_bool_word("maybe")


SECURITY_DEFAULT_ON = (
    "JWT_COOKIE_SECURE",
    "JWT_COOKIE_HTTPONLY",
    "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE",
)
SECURITY_DEFAULT_OFF = (
    "CORS_ALLOW_ALL_ORIGINS",
    "STAPEL_TRUST_PROXY_SSL_HEADER",
    "STAPEL_PUBLIC_API_SCHEMA",
    "JWT_REFRESH_ALLOWED",
)


PROJECT_SETTINGS = """
from stapel_core.django.settings import *  # noqa: F401,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
]
ROOT_URLCONF = "projurls"
"""

NAMES = SECURITY_DEFAULT_ON + SECURITY_DEFAULT_OFF + ("STAPEL_WS_ALLOW_QUERY_TOKEN",)


def _settings_values(tmp_path, env):
    """Boot a project that star-imports the shipped settings, under *env*.

    A fresh interpreter, the way test_cookie_security_defaults boots one: the
    module is a settings module and this process already has one.
    """
    (tmp_path / "projsettings.py").write_text(PROJECT_SETTINGS, encoding="utf-8")
    (tmp_path / "projurls.py").write_text("urlpatterns = []\n", encoding="utf-8")
    child_env = {k: v for k, v in os.environ.items() if k not in NAMES}
    child_env["DJANGO_SETTINGS_MODULE"] = "projsettings"
    child_env["PYTHONPATH"] = str(tmp_path)
    child_env.update(env)
    script = textwrap.dedent(f"""
        import json
        import django
        django.setup()
        from django.conf import settings
        print(json.dumps({{n: getattr(settings, n) for n in {NAMES!r}}}))
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        env=child_env, cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("raw", ["", " ", "maybe", "1", "yes", "on", "true"])
def test_shipped_cookie_gates_stay_on(tmp_path, raw):
    values = _settings_values(tmp_path, {name: raw for name in SECURITY_DEFAULT_ON})
    assert {n: values[n] for n in SECURITY_DEFAULT_ON} == dict.fromkeys(SECURITY_DEFAULT_ON, True)


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE"])
def test_shipped_cookie_gates_close_only_on_a_false_word(tmp_path, raw):
    values = _settings_values(tmp_path, {name: raw for name in SECURITY_DEFAULT_ON})
    assert {n: values[n] for n in SECURITY_DEFAULT_ON} == dict.fromkeys(SECURITY_DEFAULT_ON, False)


@pytest.mark.parametrize("raw", ["", " ", "maybe", "0", "false", "off"])
def test_shipped_hatches_stay_shut(tmp_path, raw):
    values = _settings_values(tmp_path, {name: raw for name in SECURITY_DEFAULT_OFF})
    assert {n: values[n] for n in SECURITY_DEFAULT_OFF} == dict.fromkeys(SECURITY_DEFAULT_OFF, False)


@pytest.mark.parametrize("raw,expected", [
    ("", False), ("maybe", False), ("false", False), ("true", True), ("1", True),
])
def test_ws_query_token_blank_or_garbage_is_off(tmp_path, raw, expected):
    assert _settings_values(tmp_path, {"STAPEL_WS_ALLOW_QUERY_TOKEN": raw})[
        "STAPEL_WS_ALLOW_QUERY_TOKEN"] is expected


def test_ws_query_token_unset_defers_to_the_posture(tmp_path):
    assert _settings_values(tmp_path, {})["STAPEL_WS_ALLOW_QUERY_TOKEN"] is None
