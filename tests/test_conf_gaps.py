"""AppSettings resolution order, caching and import_strings error paths."""
import pytest

from stapel_core.conf import AppSettings


def test_namespace_dict_wins(settings):
    settings.STAPEL_CONFTEST = {"GREETING": "from-namespace"}
    s = AppSettings("STAPEL_CONFTEST", defaults={"GREETING": "default"})
    assert s.GREETING == "from-namespace"


def test_flat_setting_beats_env_and_default(settings, monkeypatch):
    settings.STAPEL_CONFTEST_FLAT_KEY = "from-flat"
    monkeypatch.setenv("STAPEL_CONFTEST_FLAT_KEY", "from-env")
    s = AppSettings("STAPEL_CONFTEST", defaults={"STAPEL_CONFTEST_FLAT_KEY": "default"})
    assert s.STAPEL_CONFTEST_FLAT_KEY == "from-flat"


def test_env_fallback_beats_default(monkeypatch):
    monkeypatch.setenv("STAPEL_CONFTEST_ENV_KEY", "from-env")
    s = AppSettings("STAPEL_CONFTEST", defaults={"STAPEL_CONFTEST_ENV_KEY": "default"})
    assert s.STAPEL_CONFTEST_ENV_KEY == "from-env"


def test_default_used_when_nothing_configured():
    s = AppSettings("STAPEL_CONFTEST", defaults={"STAPEL_CONFTEST_DEF_KEY": "default"})
    assert s.STAPEL_CONFTEST_DEF_KEY == "default"


def test_unknown_key_raises():
    s = AppSettings("STAPEL_CONFTEST", defaults={})
    with pytest.raises(AttributeError, match="has no setting"):
        s.STAPEL_CONFTEST_NOPE


def test_underscore_attributes_never_resolve():
    s = AppSettings("STAPEL_CONFTEST", defaults={"_SECRET": "x"})
    with pytest.raises(AttributeError):
        s._SECRET


def test_value_is_cached_until_reload(monkeypatch):
    monkeypatch.setenv("STAPEL_CONFTEST_CACHE_KEY", "first")
    s = AppSettings("STAPEL_CONFTEST", defaults={"STAPEL_CONFTEST_CACHE_KEY": "default"})
    assert s.STAPEL_CONFTEST_CACHE_KEY == "first"
    monkeypatch.setenv("STAPEL_CONFTEST_CACHE_KEY", "second")
    assert s.STAPEL_CONFTEST_CACHE_KEY == "first"  # served from cache
    s.reload()
    assert s.STAPEL_CONFTEST_CACHE_KEY == "second"


def test_cache_invalidated_by_setting_changed_signal(settings):
    s = AppSettings("STAPEL_CONFTEST", defaults={"GREETING": "default"})
    assert s.GREETING == "default"
    settings.STAPEL_CONFTEST = {"GREETING": "overridden"}  # fires setting_changed
    assert s.GREETING == "overridden"


def test_import_strings_resolves_dotted_path():
    s = AppSettings(
        "STAPEL_CONFTEST",
        defaults={"PROVIDER": "stapel_core.conf.AppSettings"},
        import_strings=("PROVIDER",),
    )
    assert s.PROVIDER is AppSettings


def test_import_strings_bad_path_raises_import_error():
    s = AppSettings(
        "STAPEL_CONFTEST",
        defaults={"PROVIDER": "no.such.module.Thing"},
        import_strings=("PROVIDER",),
    )
    with pytest.raises(ImportError):
        s.PROVIDER


def test_import_strings_empty_value_passes_through():
    s = AppSettings(
        "STAPEL_CONFTEST",
        defaults={"PROVIDER": ""},
        import_strings=("PROVIDER",),
    )
    assert s.PROVIDER == ""
