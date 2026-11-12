"""Auto-registration of payload schemas from app `schemas/` directories.

Modules already commit their event contracts as JSON Schema files
(`schemas/emits/user.deleted.json`, `schemas/functions/cdn.media_exists.json`).
This loader walks INSTALLED_APPS and registers them with the comm
registries, so VALIDATE_SCHEMAS catches code-vs-schema drift in tests/CI
instead of in an audit.

File name = action/function name (minus .json). Called once from the
taskstore AppConfig.ready().

``schemas/emits/`` is a DECLARATION: the app shipping the file is the fact's
owner, and only owners write the emit registry (a consumer's
``on_action(schema=)`` is kept per subscription instead). Two apps declaring
one fact with different schemas are reported by ``stapel_core.comm.E011``.

Core is a library, not an app with a ``schemas/`` directory of its own: the
facts it emits ship next to their emitters (``gdpr/schemas/emits/``,
``notifications/schemas/emits/``). :func:`register_core_emit_schemas` declares
every one of them with ``owner="stapel_core"``; it runs from core's
``AppConfig.ready()`` and first thing in :func:`autoload_schemas`, so core is
the first declared owner of its facts whatever the INSTALLED_APPS order.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_loaded = False

#: Owner name core's own emit declarations are recorded under.
CORE_OWNER = "stapel_core"

_CORE_ROOT = Path(__file__).resolve().parent.parent


def core_emit_schema_files() -> dict[str, Path]:
    """``{fact: path}`` for every ``schemas/emits/*.json`` core ships."""
    return {
        path.stem: path
        for path in sorted(_CORE_ROOT.glob("*/schemas/emits/*.json"))
    }


def register_core_emit_schemas(registry=None) -> int:
    """Declare every emit schema core ships, as their owner. Idempotent.

    Returns the number declared. Re-declaring under the same owner replaces
    the schema with itself, so calling it from several ``ready()`` hooks is
    safe.
    """
    from .registry import action_registry

    registry = registry or action_registry
    count = 0
    for fact, path in core_emit_schema_files().items():
        schema = _read(path)
        if schema is not None:
            registry.register_schema(fact, schema, owner=CORE_OWNER)
            count += 1
    return count


def autoload_schemas() -> int:
    """Register emits/functions schemas from every installed app. Returns
    the number of schemas registered. Idempotent."""
    global _loaded
    if _loaded:
        return 0
    _loaded = True

    from django.apps import apps

    from .registry import action_registry, function_registry

    register_core_emit_schemas(action_registry)
    count = 0
    for app_config in apps.get_app_configs():
        base = Path(app_config.path) / "schemas"
        if not base.is_dir():
            continue
        owner = getattr(app_config, "name", None) or Path(app_config.path).name
        for schema_file in sorted(base.glob("emits/*.json")):
            schema = _read(schema_file)
            if schema is not None:
                action_registry.register_schema(schema_file.stem, schema, owner=owner)
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
