"""Tests for stapel_core.config — get_config over the CONFIG.MD manifest.

Covers CONFIG.MD parsing (tables, owners, required/default cells, bad source),
env|vault routing over the secret seam, fail-closed for required keys, manifest
discovery + cache, and that core's own shipped CONFIG.MD parses and lists its
secret keys as vault.
"""
from pathlib import Path

import pytest
from django.test import override_settings

import stapel_core.config as config
from stapel_core.config import (
    ConfigKeyUnknown,
    ConfigManifestError,
    ConfigUnavailable,
    get_config,
    load_manifest,
    parse_config_md,
    reset_manifest_cache,
)

MANIFEST_TEXT = """\
# CONFIG.MD — testproj

## stapel-core
| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| SECRET_KEY | vault | Django secret | yes | |
| JWT_SECRET_KEY | vault | JWT secret | no | |

## project
| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| LOG_LEVEL | env | Root log level | no | INFO |
| DATA_DIR | env | Where data lives | yes | |
| CORS_ALL | env | Allow all CORS | no | False |
"""


@pytest.fixture
def manifest():
    return parse_config_md(MANIFEST_TEXT)


@pytest.fixture(autouse=True)
def _reset():
    reset_manifest_cache()
    yield
    reset_manifest_cache()


# --- parsing ---------------------------------------------------------------

def test_parse_reads_all_rows(manifest):
    assert set(manifest) == {"SECRET_KEY", "JWT_SECRET_KEY", "LOG_LEVEL", "DATA_DIR", "CORS_ALL"}


def test_parse_source_required_default(manifest):
    assert manifest["SECRET_KEY"].source == "vault"
    assert manifest["SECRET_KEY"].required is True
    assert manifest["SECRET_KEY"].default is None
    assert manifest["LOG_LEVEL"].source == "env"
    assert manifest["LOG_LEVEL"].required is False
    assert manifest["LOG_LEVEL"].default == "INFO"
    assert manifest["DATA_DIR"].required is True


def test_parse_owner_headings(manifest):
    assert manifest["SECRET_KEY"].owner == "stapel-core"
    assert manifest["LOG_LEVEL"].owner == "project"


def test_parse_bad_source_raises():
    bad = "| Key | Source |\n|-----|--------|\n| X | s3 |\n"
    with pytest.raises(ConfigManifestError):
        parse_config_md(bad)


def test_parse_ignores_prose_tables():
    # A table without a key+source header is not a manifest table.
    text = "| Name | Age |\n|------|-----|\n| Bob | 9 |\n"
    assert parse_config_md(text) == {}


# --- env routing -----------------------------------------------------------

def test_env_source_reads_environ(monkeypatch, manifest):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert get_config("LOG_LEVEL", manifest=manifest) == "DEBUG"


def test_env_source_uses_manifest_default(monkeypatch, manifest):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert get_config("LOG_LEVEL", manifest=manifest) == "INFO"


def test_env_source_caller_default_wins(monkeypatch, manifest):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert get_config("LOG_LEVEL", "WARNING", manifest=manifest) == "WARNING"


def test_env_required_missing_fails_closed(monkeypatch, manifest):
    monkeypatch.delenv("DATA_DIR", raising=False)
    with pytest.raises(ConfigUnavailable):
        get_config("DATA_DIR", manifest=manifest)


def test_env_required_with_caller_default_ok(monkeypatch, manifest):
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert get_config("DATA_DIR", "/tmp/data", manifest=manifest) == "/tmp/data"


# --- vault routing (delegates to get_secret / provider seam) ---------------

def test_vault_source_delegates_to_provider(manifest):
    with override_settings(
        STAPEL_SECRETS={"PROVIDER": "tests.test_config._DictProvider"}
    ):
        assert get_config("SECRET_KEY", manifest=manifest) == "from-vault"


def test_vault_required_missing_fails_closed(manifest):
    # A fail-closed provider with no value + required key -> ConfigUnavailable.
    with override_settings(
        STAPEL_SECRETS={"PROVIDER": "tests.test_config._EmptyProvider"}
    ):
        with pytest.raises(ConfigUnavailable):
            get_config("SECRET_KEY", manifest=manifest)


def test_vault_default_short_circuits(manifest):
    with override_settings(
        STAPEL_SECRETS={"PROVIDER": "tests.test_config._EmptyProvider"}
    ):
        assert get_config("JWT_SECRET_KEY", "fallback", manifest=manifest) == "fallback"


def test_vault_env_provider_reads_environ(monkeypatch, manifest):
    # Default env provider: a vault-source key still resolves from os.environ.
    monkeypatch.setenv("JWT_SECRET_KEY", "env-jwt")
    assert get_config("JWT_SECRET_KEY", manifest=manifest) == "env-jwt"


# --- unknown key -----------------------------------------------------------

def test_unknown_key_raises(manifest):
    with pytest.raises(ConfigKeyUnknown):
        get_config("NOT_DECLARED", manifest=manifest)


def test_unknown_key_with_default_returns_default(manifest):
    assert get_config("NOT_DECLARED", "d", manifest=manifest) == "d"


# --- discovery + cache -----------------------------------------------------

def test_discovery_via_env_var(tmp_path, monkeypatch):
    md = tmp_path / "CONFIG.MD"
    md.write_text(MANIFEST_TEXT, encoding="utf-8")
    monkeypatch.setenv("STAPEL_CONFIG_MANIFEST", str(md))
    monkeypatch.setenv("LOG_LEVEL", "TRACE")
    reset_manifest_cache()
    assert get_config("LOG_LEVEL") == "TRACE"


def test_missing_manifest_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("STAPEL_CONFIG_MANIFEST", str(tmp_path / "nope.md"))
    reset_manifest_cache()
    assert load_manifest() == {}


# --- core's own shipped registry -------------------------------------------

def test_core_config_md_parses_and_marks_secrets_vault():
    core_md = Path(config.__file__).resolve().parent.parent / "CONFIG.MD"
    table = parse_config_md(core_md)
    assert table["SECRET_KEY"].source == "vault"
    assert table["SECRET_KEY"].required is True
    assert table["LOG_LEVEL"].source == "env"
    # every row is owned by stapel-core
    assert {e.owner for e in table.values()} == {"stapel-core"}


class _DictProvider:
    fail_closed = True

    def get(self, name):
        return {"SECRET_KEY": "from-vault"}.get(name)


class _EmptyProvider:
    fail_closed = True

    def get(self, name):
        return None
