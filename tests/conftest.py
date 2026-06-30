# Configure Django before any test imports that touch it.
from stapel_core.testing import configure_django

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
