"""comm.schemas autoloader: registration, idempotency, unreadable files."""
import json
from types import SimpleNamespace

import pytest

from stapel_core.comm import schemas as schemas_mod
from stapel_core.comm.registry import action_registry, function_registry


@pytest.fixture(autouse=True)
def reset():
    schemas_mod.reset_autoload()
    action_registry.clear()
    function_registry.clear()
    yield
    schemas_mod.reset_autoload()
    action_registry.clear()
    function_registry.clear()


def _app_with_schemas(tmp_path):
    (tmp_path / "schemas" / "emits").mkdir(parents=True)
    (tmp_path / "schemas" / "functions").mkdir()
    (tmp_path / "schemas" / "emits" / "user.deleted.json").write_text(
        json.dumps({"type": "object"})
    )
    (tmp_path / "schemas" / "functions" / "cdn.media_exists.json").write_text(
        json.dumps({"type": "object"})
    )
    return SimpleNamespace(path=str(tmp_path))


def _patch_apps(monkeypatch, app_configs):
    from django.apps import apps

    monkeypatch.setattr(apps, "get_app_configs", lambda: app_configs)


def test_autoload_registers_emits_and_functions(tmp_path, monkeypatch):
    no_schemas_dir = tmp_path / "plain_app"
    no_schemas_dir.mkdir()
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    _patch_apps(
        monkeypatch,
        [_app_with_schemas(app_dir), SimpleNamespace(path=str(no_schemas_dir))],
    )

    count = schemas_mod.autoload_schemas()
    assert count == 2
    assert action_registry._schemas["user.deleted"] == {"type": "object"}
    assert function_registry._schemas["cdn.media_exists"] == {"type": "object"}


def test_autoload_is_idempotent(tmp_path, monkeypatch):
    _patch_apps(monkeypatch, [_app_with_schemas(tmp_path)])
    assert schemas_mod.autoload_schemas() == 2
    assert schemas_mod.autoload_schemas() == 0  # second call is a no-op
    # reset_autoload re-arms it (tests only)
    schemas_mod.reset_autoload()
    assert schemas_mod.autoload_schemas() == 2


def test_unreadable_schema_is_skipped(tmp_path, monkeypatch):
    app = _app_with_schemas(tmp_path)
    (tmp_path / "schemas" / "emits" / "broken.json").write_text("{not json")
    _patch_apps(monkeypatch, [app])
    count = schemas_mod.autoload_schemas()
    assert count == 2  # broken.json skipped, valid ones registered
    assert "broken" not in action_registry._schemas
