import os as _os
import sys as _sys

# The flat package layout (package-dir={"stapel_core":"."}) places django/ at the repo
# root. pytest adds conftest parent directories to sys.path, so `import django` resolves
# to the local django/ package directory instead of the installed Django framework.
# Remove the repo root from sys.path before any imports to prevent this shadowing.
_repo_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _repo_root]

# Configure Django before any test imports that touch it.
from stapel_core.testing import configure_django  # noqa: E402

configure_django(
    installed_apps=["stapel_core.django.users"],
    extra_settings={
        "AUTH_USER_MODEL": "users.User",
        "CACHES": {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        "STAPEL_BUS_BACKEND": "stapel_core.bus.backends.memory.MemoryBus",
    },
)

import pytest  # noqa: E402
from stapel_core.bus import reset_bus  # noqa: E402


@pytest.fixture(autouse=True)
def reset_bus_singleton():
    reset_bus()
    yield
    reset_bus()


@pytest.fixture(autouse=True)
def clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()
