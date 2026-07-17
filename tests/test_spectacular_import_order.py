"""Regression test for the drf-spectacular import-order bug.

Root cause (see ``stapel_core.django.apps._unpoison_spectacular_settings``):
importing ``stapel_core.django`` eagerly pulls in ``drf_spectacular.openapi.
AutoSchema`` (via ``openapi/schemas.py``), which cascades into importing
``drf_spectacular.settings`` — a module whose body constructs the
process-wide ``spectacular_settings`` singleton by snapshotting
``django.conf.settings.SPECTACULAR_SETTINGS`` *at import time*. Projects that
write their settings module as::

    from stapel_core.django.settings import *   # triggers the cascade above
    ...
    SPECTACULAR_SETTINGS = get_spectacular_settings(...)   # further down

end up with the singleton snapshotting an empty ``SPECTACULAR_SETTINGS``
(the project's real assignment hasn't executed yet), permanently pinning
``TITLE=''`` / ``VERSION='0.0.0'`` for the rest of the process —
drf-spectacular never re-reads the setting afterwards.

This test reproduces that exact ordering (singleton built before the real
setting is visible), then verifies ``CommonDjangoConfig.ready()``'s
``_unpoison_spectacular_settings`` patches the already-built singleton back
in line with the project's actual ``SPECTACULAR_SETTINGS`` — the same
apply_patches seam the stapel-example-monolith previously had to apply
locally in its own ``AppConfig.ready()`` before this framework-level fix.
"""
import importlib
import sys

import pytest
from django.test import override_settings

from stapel_core.django.apps import _unpoison_spectacular_settings


def _reimport_spectacular_settings_module():
    """Force a fresh import of ``drf_spectacular.settings``, snapshotting
    whatever ``SPECTACULAR_SETTINGS`` is visible on ``django.conf.settings``
    at the moment of import — exactly what happens the first time any code
    path (e.g. ``stapel_core.django.openapi.schemas``) imports it."""
    sys.modules.pop("drf_spectacular.settings", None)
    return importlib.import_module("drf_spectacular.settings")


@pytest.fixture
def broken_order_singleton():
    """Simulate 'singleton built before settings' — the exact broken order.

    Rebuilds the drf-spectacular settings singleton while
    ``SPECTACULAR_SETTINGS`` is unset (empty defaults: ``TITLE=''``,
    ``VERSION='0.0.0'``), mirroring a project settings module that imports
    ``stapel_core.django.settings`` (triggering the eager
    ``drf_spectacular.settings`` import) *before* reaching its own
    ``SPECTACULAR_SETTINGS = get_spectacular_settings(...)`` line.

    Restores the singleton to a clean, correctly-ordered state afterwards so
    this test can't leak a poisoned global into the rest of the suite.
    """
    # The ambient test settings (tests/conftest.py) never set
    # SPECTACULAR_SETTINGS, so importing fresh here reproduces exactly what
    # happens when a project's settings module hasn't reached its own
    # ``SPECTACULAR_SETTINGS = get_spectacular_settings(...)`` line yet.
    from django.conf import settings as django_settings

    assert getattr(django_settings, "SPECTACULAR_SETTINGS", None) is None

    module = _reimport_spectacular_settings_module()
    poisoned = module.spectacular_settings
    assert poisoned.TITLE == ""
    assert poisoned.VERSION == "0.0.0"

    yield poisoned

    # Cleanup: rebuild once more under real (whatever conftest configured)
    # settings so later tests in the session see a sane singleton.
    _reimport_spectacular_settings_module()


class TestSpectacularImportOrderBug:
    def test_poisoned_singleton_is_patched_from_real_settings(self, broken_order_singleton):
        real = {
            "TITLE": "My Real Project",
            "VERSION": "3.2.1",
            "DESCRIPTION": "Real description",
        }
        with override_settings(SPECTACULAR_SETTINGS=real):
            patches = _unpoison_spectacular_settings()

            assert patches == real
            assert broken_order_singleton.TITLE == "My Real Project"
            assert broken_order_singleton.VERSION == "3.2.1"
            assert broken_order_singleton.DESCRIPTION == "Real description"

    def test_revert_proof_without_patch_bug_reproduces(self, broken_order_singleton):
        """Sanity check that the fixture actually reproduces the bug: with
        no patch applied, the singleton stays pinned to the empty defaults
        even though real settings are now in effect. This is the assertion
        that goes RED if ``_unpoison_spectacular_settings`` is reverted from
        being called anywhere and nothing else fixes the singleton up."""
        real = {"TITLE": "My Real Project", "VERSION": "3.2.1"}
        with override_settings(SPECTACULAR_SETTINGS=real):
            # No call to _unpoison_spectacular_settings() here.
            assert broken_order_singleton.TITLE == ""
            assert broken_order_singleton.VERSION == "0.0.0"

    def test_idempotent_second_call_is_a_noop(self, broken_order_singleton):
        real = {"TITLE": "My Real Project", "VERSION": "3.2.1"}
        with override_settings(SPECTACULAR_SETTINGS=real):
            first = _unpoison_spectacular_settings()
            assert first == real

            second = _unpoison_spectacular_settings()
            assert second == {}
            assert broken_order_singleton.TITLE == "My Real Project"
            assert broken_order_singleton.VERSION == "3.2.1"

    def test_zero_effect_when_order_was_correct(self):
        """If the singleton is (re)built *after* SPECTACULAR_SETTINGS is
        already set — the correct order — the values already match and no
        patch is applied."""
        real = {"TITLE": "Correctly Ordered", "VERSION": "1.0.0"}
        with override_settings(SPECTACULAR_SETTINGS=real):
            module = _reimport_spectacular_settings_module()
            assert module.spectacular_settings.TITLE == "Correctly Ordered"

            patches = _unpoison_spectacular_settings()

            assert patches == {}
            assert module.spectacular_settings.TITLE == "Correctly Ordered"

        # Cleanup: rebuild once more so this test doesn't leak state either.
        _reimport_spectacular_settings_module()

    def test_missing_drf_spectacular_is_a_noop(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "drf_spectacular.settings" or name.startswith("drf_spectacular.settings."):
                raise ImportError("simulated: drf-spectacular not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        assert _unpoison_spectacular_settings() == {}
