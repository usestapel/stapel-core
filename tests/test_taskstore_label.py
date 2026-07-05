"""Regression: the internal taskstore app label must not collide with the
generic ``stapel-tasks`` module.

stapel-core's background comm-Task persistence app historically used the Django
label ``stapel_tasks``. The user-facing task/kanban module ``stapel-tasks``
(0.1.0) owns that canonical label, so core 0.8.0 renamed its taskstore label to
``stapel_taskstore`` (docs/tasks-module.md §2/§11). This test proves both apps
now boot in a single INSTALLED_APPS.

Django can only be configured once per process and the suite's conftest already
configures it without ``stapel_tasks``; so the collision check runs in a fresh
subprocess.
"""
import subprocess
import sys

import pytest


def _has_stapel_tasks() -> bool:
    import importlib.util

    return importlib.util.find_spec("stapel_tasks") is not None


BOOT_BOTH = """
import os, sys
sys.path = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != os.path.abspath(%r)]
import django
from django.conf import settings
settings.configure(
    SECRET_KEY="x",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    INSTALLED_APPS=[
        "django.contrib.contenttypes", "django.contrib.auth", "rest_framework",
        "stapel_core.django.users", "stapel_core.django.outbox",
        "stapel_core.django.taskstore",
        "stapel_tasks",
    ],
    AUTH_USER_MODEL="users.User",
    ROOT_URLCONF="", ALLOWED_HOSTS=["*"], USE_TZ=True,
    STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
)
django.setup()
from django.apps import apps
store = apps.get_app_config("stapel_taskstore")
tasks = apps.get_app_config("stapel_tasks")
assert store.label == "stapel_taskstore", store.label
assert tasks.label == "stapel_tasks", tasks.label
assert store.name == "stapel_core.django.taskstore", store.name
assert tasks.name == "stapel_tasks", tasks.name
# Physical table stays on its historical name (label-only rename, no data move).
from stapel_core.django.taskstore.models import TaskRecord
assert TaskRecord._meta.db_table == "stapel_tasks_taskrecord", TaskRecord._meta.db_table
print("BOTH_LIVE_OK")
"""


@pytest.mark.skipif(not _has_stapel_tasks(), reason="stapel-tasks not installed")
def test_taskstore_and_tasks_module_coexist():
    # Run in a clean interpreter: Django is already configured in this process.
    # The subprocess must strip the core repo root from sys.path exactly like
    # conftest does, so ``import django`` finds the framework, not core's
    # ``django/`` package dir. Pass the repo root as the arg to the strip.
    import os

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = BOOT_BOTH % (repo_root,)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "BOTH_LIVE_OK" in result.stdout, result.stdout


def test_taskstore_label_is_renamed():
    """In-process guard: the app config carried by the running suite already
    uses the new label and the historical table name."""
    from django.apps import apps

    cfg = apps.get_app_config("stapel_taskstore")
    assert cfg.name == "stapel_core.django.taskstore"
    from stapel_core.django.taskstore.models import TaskRecord

    assert TaskRecord._meta.db_table == "stapel_tasks_taskrecord"
    # The old label must no longer be registered.
    with pytest.raises(LookupError):
        apps.get_app_config("stapel_tasks")
