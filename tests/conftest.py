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
    installed_apps=["django.contrib.staticfiles", "stapel_core.django.users", "stapel_core.django.outbox", "stapel_core.django.taskstore", "stapel_core.django.eventstore", "stapel_core.django.projections", "stapel_core.django.gateway"],
    extra_settings={
        "AUTH_USER_MODEL": "users.User",
        # django.contrib.staticfiles refuses to start without it, and the
        # embedded-static check needs the real finders, not a stub.
        "STATIC_URL": "/static/",
        "STATICFILES_DIRS": [],
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

# The core's own suite runs the emit gate it ships (stapel_core.testing): an
# emit() at the atomic depth a test started at raises instead of quietly
# writing a detached outbox row. Autouse — importing it is the installation.
# A test that means to emit at baseline depth asks for the sibling fixture
# emit_outside_atomic_allowed.
from stapel_core.testing import (  # noqa: E402,F401
    emit_outside_atomic_allowed,
    emit_outside_atomic_gate,
)


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


@pytest.fixture(scope="session", autouse=True)
def error_registry_is_whole():
    """Import every error module once, before any test can be the one that does.

    Several core modules register their keys at import, and the i18n gates
    force those imports on first use — so which test paid for them, and which
    keys the registry holds while a test runs, depended on the file order.
    Paying once up front makes the registry the same for every test.
    """
    from stapel_core.i18n import source_owners

    source_owners("errors")


@pytest.fixture(autouse=True)
def restore_error_registries(error_registry_is_whole):
    """Error keys are registered process-globally; put the registry back.

    ``register_service_errors`` writes into four module-level dicts that every
    later test reads, and the drift gates in tests/test_error_i18n.py read them
    as the fleet's catalogue: a probe key registered by an earlier test is
    reported there as a core key nobody translated, and an overridden baseline
    text as a translation gone stale. Restored around every test rather than
    on request, because an opt-in sandbox is a rule each new test has to
    remember, and the tests that forgot it were only red in one file ordering.
    """
    from stapel_core.django.api import errors as errors_module

    names = (
        "_GLOBAL_REGISTRY",
        "_LANGUAGE_REGISTRY",
        "_REMEDIATION_REGISTRY",
        "_OWNER_REGISTRY",
    )
    saved = {name: dict(getattr(errors_module, name)) for name in names}
    yield
    for name, snapshot in saved.items():
        live = getattr(errors_module, name)
        live.clear()
        live.update(snapshot)


@pytest.fixture(autouse=True)
def drop_eventstore_buffer():
    """Discard whatever a test left in the event store's write buffer.

    The buffer is one process-global object. A test that appends without a
    database leaves its events pending in it, and the next ``override_settings``
    of an eventstore key fires ``_reset_state``, which FLUSHES — so those events
    land in whichever test's database is open at that moment and are counted by
    its assertions. Dropped rather than flushed here: flushing outside a
    database-enabled test is what produced them in the wrong place to begin
    with.
    """
    yield
    from stapel_core import eventstore

    with eventstore._lock:
        eventstore._buffer = None
        eventstore._backends.clear()
        eventstore._default_backend = None
