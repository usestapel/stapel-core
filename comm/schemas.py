"""Auto-registration of payload schemas from app `schemas/` directories.

Modules already commit their event contracts as JSON Schema files
(`schemas/emits/user.deleted.json`, `schemas/functions/cdn.media_exists.json`).
This loader walks INSTALLED_APPS and registers them with the comm
registries, so VALIDATE_SCHEMAS catches code-vs-schema drift in tests/CI
instead of in an audit.

File name = action/function name (minus .json). Called once from the
taskstore AppConfig.ready().
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_loaded = False


def autoload_schemas() -> int:
    """Register emits/functions schemas from every installed app. Returns
    the number of schemas registered. Idempotent."""
    global _loaded
    if _loaded:
        return 0
    _loaded = True

    from django.apps import apps

    from .registry import action_registry, function_registry

    count = 0
    for app_config in apps.get_app_configs():
        base = Path(app_config.path) / "schemas"
        if not base.is_dir():
            continue
        for schema_file in sorted(base.glob("emits/*.json")):
            schema = _read(schema_file)
            if schema is not None:
                action_registry.register_schema(schema_file.stem, schema)
                count += 1
        for schema_file in sorted(base.glob("functions/*.json")):
            schema = _read(schema_file)
            if schema is not None:
                function_registry.register_schema(schema_file.stem, schema)
                count += 1
    if count:
        logger.debug("comm: auto-registered %d payload schema(s)", count)
    return count


def reset_autoload() -> None:
    """Tests only."""
    global _loaded
    _loaded = False


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        logger.warning("comm: unreadable schema %s — skipped", path)
        return None
