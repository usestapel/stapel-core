"""Tests for stapel_core.config.checks — the boot-time required-config gate.

Before this check, ``required`` in CONFIG.MD was only enforced the first
time some code path called ``get_config(key)`` — possibly deep in a request
handler, possibly never. ``manage.py check`` now walks every required key
(manifest rows ∪ call-site ``declare_config`` declarations) and fails E-level
when one has no value and no default, at boot-smoke time.

Isolated from stapel-core's own real CONFIG.MD via ``STAPEL_CONFIG_MANIFEST``
+ a throwaway manifest file, so these tests never depend on which of core's
44 real keys happen to be set in the ambient test/CI environment.
"""
from pathlib import Path

import pytest

from stapel_core.config import (
    clear_declared_config,
    declare_config,
    reset_manifest_cache,
)
from stapel_core.config.checks import (
    E001_REQUIRED_CONFIG_MISSING,
    check_required_config,
)
from stapel_core.secrets import invalidate_secret

MANIFEST_TEXT = """\
# CONFIG.MD — testproj

## project
| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| DATA_DIR | env | Where data lives | yes | |
| LOG_LEVEL | env | Root log level | no | INFO |
| API_TOKEN | vault | Upstream API token | yes | |
"""


@pytest.fixture(autouse=True)
def _isolated_manifest(tmp_path, monkeypatch):
    md = tmp_path / "CONFIG.MD"
    md.write_text(MANIFEST_TEXT, encoding="utf-8")
    monkeypatch.setenv("STAPEL_CONFIG_MANIFEST", str(md))
    reset_manifest_cache()
    clear_declared_config()
    invalidate_secret()  # API_TOKEN is vault-routed — drop the process cache
    yield
    reset_manifest_cache()
    clear_declared_config()
    invalidate_secret()


def test_required_key_missing_fails_check(monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)

    errors = check_required_config()

    ids = {e.id for e in errors}
    assert ids == {E001_REQUIRED_CONFIG_MISSING}
    messages = " ".join(e.msg for e in errors)
    assert "DATA_DIR" in messages
    assert "API_TOKEN" in messages
    # purpose surfaces in the message ("needed for: ...")
    assert "Where data lives" in messages


def test_required_key_present_passes_check(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/srv/data")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    assert check_required_config() == []


def test_optional_key_missing_never_flagged(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("DATA_DIR", "/srv/data")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    # LOG_LEVEL has a default (INFO) and is not required — never an error,
    # regardless of whether the env var is set.
    assert check_required_config() == []


def test_partial_missing_reports_only_the_missing_one(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/srv/data")
    monkeypatch.delenv("API_TOKEN", raising=False)

    errors = check_required_config()

    assert len(errors) == 1
    assert "API_TOKEN" in errors[0].msg
    assert "DATA_DIR" not in errors[0].msg


def test_call_site_declared_required_key_also_gated(monkeypatch):
    """A key not yet in CONFIG.MD, but declared required at a call site
    (declare_config, or get_config(required=True) as a backstop), is still
    gated — required is required, declared in the table or not."""
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("API_TOKEN", "secret-token")
    declare_config("BRAND_NEW_SECRET", purpose="Not in CONFIG.MD yet", required=True)
    monkeypatch.delenv("BRAND_NEW_SECRET", raising=False)

    errors = check_required_config()

    ids_msgs = {e.msg for e in errors}
    assert any("BRAND_NEW_SECRET" in m for m in ids_msgs)
    assert any("Not in CONFIG.MD yet" in m for m in ids_msgs)


def test_manifest_wins_over_declared_on_same_key(monkeypatch):
    """A call-site declaration must not shadow a real CONFIG.MD row for the
    same key — the hand-reviewed table is authoritative."""
    monkeypatch.setenv("DATA_DIR", "/srv/data")
    monkeypatch.setenv("API_TOKEN", "secret-token")
    # Pretend some call site wrongly thinks DATA_DIR is optional/no-purpose —
    # the real manifest row (required=yes) must still be the one enforced.
    declare_config("DATA_DIR", purpose="wrong purpose", required=False)

    assert check_required_config() == []

    monkeypatch.delenv("DATA_DIR", raising=False)
    errors = check_required_config()
    assert any("DATA_DIR" in e.msg and "Where data lives" in e.msg for e in errors)


def test_core_own_config_md_has_required_keys_that_pass_when_set(monkeypatch, tmp_path):
    """Sanity check against the real, shipped CONFIG.MD (not the throwaway
    fixture above) — SECRET_KEY (vault, required) passes once its env var
    (the default EnvSecretProvider) is set."""
    import stapel_core.config as config

    core_md = Path(config.__file__).resolve().parent.parent / "CONFIG.MD"
    monkeypatch.setenv("STAPEL_CONFIG_MANIFEST", str(core_md))
    monkeypatch.setenv("SECRET_KEY", "a-real-secret-key-value")
    reset_manifest_cache()

    errors = check_required_config()
    assert not any(e.id == E001_REQUIRED_CONFIG_MISSING and "SECRET_KEY" in e.msg for e in errors)


# --- settings source --------------------------------------------------------

SETTINGS_MANIFEST_TEXT = """\
# CONFIG.MD — testproj

## stapel-cdn
| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| WATERMARKS | settings | per-site watermarks | no | `{}` |
| WATERMARK_PROFILE | settings | watermark profile | yes | |
| WATERMARK_ASSET_TYPES | settings | watermarked types | yes | `()` |
"""


@pytest.fixture
def settings_manifest(tmp_path, monkeypatch):
    md = tmp_path / "CONFIG.MD"
    md.write_text(SETTINGS_MANIFEST_TEXT, encoding="utf-8")
    monkeypatch.setenv("STAPEL_CONFIG_MANIFEST", str(md))
    reset_manifest_cache()
    return md


def _settings_errors():
    from stapel_core.config.checks import E002_REQUIRED_SETTING_MISSING

    return [e for e in check_required_config() if e.id == E002_REQUIRED_SETTING_MISSING]


def test_settings_source_manifest_does_not_crash_check(settings_manifest):
    """0.92.0 raised ConfigManifestError here: `settings` was not a source,
    so `manage.py check` crashed for any project aggregating such a row."""
    from django.test import override_settings

    with override_settings(STAPEL_CDN={"WATERMARK_PROFILE": "brand"}):
        assert check_required_config() == []


def test_settings_source_resolves_through_flat_setting(settings_manifest):
    from django.test import override_settings

    with override_settings(WATERMARK_PROFILE="brand"):
        assert _settings_errors() == []


def test_settings_source_missing_reports_row(settings_manifest, monkeypatch):
    # An env var of the same name must not satisfy a settings row.
    monkeypatch.setenv("WATERMARK_PROFILE", "from-env")

    errors = _settings_errors()

    assert len(errors) == 1
    err = errors[0]
    assert "WATERMARK_PROFILE" in err.msg
    assert "watermark profile" in err.msg
    assert "STAPEL_CDN['WATERMARK_PROFILE']" in err.hint
    assert f"{settings_manifest}:7" in err.hint
    assert "## stapel-cdn" in err.hint


def test_settings_source_row_default_satisfies_required(settings_manifest):
    # WATERMARK_ASSET_TYPES is required with a `()` default: never flagged.
    assert all("WATERMARK_ASSET_TYPES" not in e.msg for e in _settings_errors())


def test_settings_source_declared_at_call_site_is_gated(settings_manifest):
    declare_config("CALL_SITE_SETTING", source="settings", required=True)

    errors = _settings_errors()

    assert any("CALL_SITE_SETTING" in e.msg and "declare_config" in e.hint for e in errors)
