"""A REST_FRAMEWORK key a deployment sets must be in force, or say so loudly.

The regression at the centre of this file (``test_the_import_order_trap_is_
repaired``) runs in a subprocess against a real settings module, because the
defect cannot be reproduced any other way: what springs it is Django reading
a half-built ``Settings`` object while the settings module is still executing
its own body. Before this release that test fails —
``APIView.metadata_class`` comes back as DRF's ``SimpleMetadata`` while
``api_settings.DEFAULT_METADATA_CLASS`` is the project's class.
"""
import importlib
import json
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

from django.core import checks

from stapel_core.django.drf_rebind import (
    DECLARED_BINDS,
    Bind,
    binds_to_repair,
    derive_binds,
    imported_drf_modules,
    rebind_api_settings,
    same_value,
)
from stapel_core.django.drf_rebind_checks import (
    E001_SETTING_NOT_IN_FORCE,
    E002_SETTING_UNRESOLVABLE,
    W001_SOURCE_UNREADABLE,
    check_rest_framework_settings_in_force,
)

TESTS_DIR = Path(__file__).resolve().parent


def _ids(findings):
    return [f.id for f in findings]


# --------------------------------------------------------------------------
# The regression: the import-order trap, sprung for real.
# --------------------------------------------------------------------------

PROBE = """
import json
import django

django.setup()

from rest_framework.generics import GenericAPIView
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.settings import api_settings
from rest_framework.views import APIView


def name(value):
    if isinstance(value, type):
        return value.__module__ + "." + value.__name__
    return value


print("@@" + json.dumps({
    "metadata_class": name(APIView.metadata_class),
    "metadata_setting": name(api_settings.DEFAULT_METADATA_CLASS),
    "versioning_class": name(APIView.versioning_class),
    "versioning_setting": name(api_settings.DEFAULT_VERSIONING_CLASS),
    "pagination_class": name(GenericAPIView.pagination_class),
    "pagination_setting": name(api_settings.DEFAULT_PAGINATION_CLASS),
    "default_limit": LimitOffsetPagination.default_limit,
    "page_size_setting": api_settings.PAGE_SIZE,
    "tags": sorted(django.core.checks.registry.registry.tags_available()),
    "checks": sorted(
        m.id for m in django.core.checks.run_checks(tags=["stapel_drf_settings"])
    ),
}))
"""


def _run_probe(script=PROBE):
    """``django.setup()`` on tests/drf_import_order_settings.py, out of process."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TESTS_DIR)
    env["DJANGO_SETTINGS_MODULE"] = "drf_import_order_settings"
    result = subprocess.run(
        [sys.executable, "-c", "import django.core.checks\n" + script],
        capture_output=True,
        text=True,
        cwd=str(TESTS_DIR),
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = [line for line in result.stdout.splitlines() if line.startswith("@@")]
    assert payload, result.stdout + result.stderr
    return json.loads(payload[-1][2:])


def test_the_import_order_trap_is_repaired():
    """The named defect: DEFAULT_METADATA_CLASS set below the DRF import.

    This is the test that fails on the code before this release — the setting
    resolves correctly and the class attribute every view inherits does not.
    """
    seen = _run_probe()
    assert seen["metadata_setting"] == "drf_import_order_settings.CustomMetadata"
    assert seen["metadata_class"] == seen["metadata_setting"]


def test_the_trap_is_repaired_past_APIView_too():
    """GenericAPIView.pagination_class and the paginator's own PAGE_SIZE.

    Neither is on APIView, and neither was in the two-attribute repair: a
    deployment that configured pagination got none, silently.
    """
    seen = _run_probe()
    assert seen["pagination_setting"] == "rest_framework.pagination.LimitOffsetPagination"
    assert seen["pagination_class"] == seen["pagination_setting"]
    assert seen["page_size_setting"] == 33
    assert seen["default_limit"] == 33
    assert seen["versioning_class"] == seen["versioning_setting"]


def test_a_repaired_deployment_reports_nothing():
    """The check runs in that deployment, and is silent on it.

    The tag assertion is the load-bearing half: an empty finding list from a
    check that was never registered is the shape of gate this repo keeps
    finding in other people's code.
    """
    seen = _run_probe()
    assert "stapel_drf_settings" in seen["tags"]
    assert seen["checks"] == []


# --------------------------------------------------------------------------
# Derivation: the set is read out of the installed DRF, not remembered.
# --------------------------------------------------------------------------


def _import_all_of_drf():
    import rest_framework

    for module in pkgutil.walk_packages(rest_framework.__path__, "rest_framework."):
        try:
            importlib.import_module(module.name)
        except Exception:
            # e.g. rest_framework.authtoken.admin with the app not installed.
            continue


def test_declared_floor_matches_the_installed_drf():
    """The drift gate: a DRF that adds a bind turns THIS red.

    ``DECLARED_BINDS`` is only the fallback for an unreadable install, but it
    is also the statement "this is what DRF does", and a statement nothing
    checks is a statement that goes stale — which is exactly how the
    two-attribute repair survived for years. Derivation covers a new
    attribute at runtime with no release; this makes the release happen.
    """
    _import_all_of_drf()
    derived, unreadable = derive_binds()
    assert unreadable == []
    assert set(derived) == set(DECLARED_BINDS), {
        "only in the installed DRF": sorted(
            str(b) for b in set(derived) - set(DECLARED_BINDS)
        ),
        "only in DECLARED_BINDS": sorted(
            str(b) for b in set(DECLARED_BINDS) - set(derived)
        ),
    }


def test_derivation_reads_class_bodies_only():
    """A module-level or function-local read of api_settings is not a bind.

    ``rest_framework.urlpatterns`` assigns ``suffix_kwarg =
    api_settings.FORMAT_SUFFIX_KWARG`` inside a function, where it is
    re-evaluated on every call and there is nothing stale to repair.
    """
    importlib.import_module("rest_framework.urlpatterns")
    derived, _ = derive_binds(["rest_framework.urlpatterns"])
    assert derived == []


def test_derivation_finds_annotated_and_plain_assignments(monkeypatch):
    source = (
        "class A:\n"
        "    plain = api_settings.ONE\n"
        "    annotated: int = api_settings.TWO\n"
        "    a = b = api_settings.THREE\n"
        "    not_a_bind = other_settings.FOUR\n"
        "    also_not = api_settings\n"
        "    def method(self):\n"
        "        local = api_settings.FIVE\n"
        "class Outer:\n"
        "    class Inner:\n"
        "        nested = api_settings.SIX\n"
    )
    monkeypatch.setattr(
        "stapel_core.django.drf_rebind._module_source",
        lambda name: source,
    )
    derived, unreadable = derive_binds(["rest_framework.pretend"])
    assert unreadable == []
    assert {(b.cls, b.attr, b.setting) for b in derived} == {
        ("A", "plain", "ONE"),
        ("A", "annotated", "TWO"),
        ("A", "a", "THREE"),
        ("A", "b", "THREE"),
    }


def test_only_imported_modules_are_considered():
    """A DRF module nobody imported binds correctly on its own later."""
    for name in imported_drf_modules():
        assert name == "rest_framework" or name.startswith("rest_framework.")
    assert "rest_framework.views" in imported_drf_modules()


def test_an_unreadable_module_falls_back_to_the_declared_floor(monkeypatch):
    monkeypatch.setattr(
        "stapel_core.django.drf_rebind.imported_drf_modules",
        lambda: ["rest_framework.views"],
    )
    monkeypatch.setattr(
        "stapel_core.django.drf_rebind._module_source",
        lambda name: None,
    )
    binds, unreadable = binds_to_repair()
    assert unreadable == ["rest_framework.views"]
    assert {b.attr for b in binds} == {
        b.attr for b in DECLARED_BINDS if b.module == "rest_framework.views"
    }


# --------------------------------------------------------------------------
# The repair itself.
# --------------------------------------------------------------------------


def test_rebind_is_idempotent():
    rebind_api_settings()
    assert rebind_api_settings() == []


def test_rebind_repairs_a_stale_attribute_and_names_it(monkeypatch):
    from rest_framework.settings import api_settings
    from rest_framework.views import APIView

    from rest_framework.metadata import BaseMetadata

    class Stale(BaseMetadata):
        pass

    monkeypatch.setattr(APIView, "metadata_class", Stale)
    changed = rebind_api_settings()

    assert Bind(
        "rest_framework.views", "APIView", "metadata_class", "DEFAULT_METADATA_CLASS"
    ) in changed
    assert APIView.metadata_class is api_settings.DEFAULT_METADATA_CLASS


def test_rebind_leaves_a_subclass_that_declares_its_own_alone(monkeypatch):
    from rest_framework.metadata import BaseMetadata
    from rest_framework.views import APIView

    class Mine(BaseMetadata):
        pass

    class MyView(APIView):
        metadata_class = Mine

    rebind_api_settings()
    assert MyView.metadata_class is Mine


def test_same_value_ignores_list_versus_tuple():
    assert same_value([1, 2], (1, 2))
    assert not same_value([1, 2], [2, 1])
    assert same_value(None, None)


# --------------------------------------------------------------------------
# The loud half.
# --------------------------------------------------------------------------


def test_check_is_silent_on_a_healthy_process():
    rebind_api_settings()
    assert check_rest_framework_settings_in_force() == []


def test_check_errors_when_a_configured_key_is_not_in_force(monkeypatch):
    """The live defect's signature: the setting resolves, the class disagrees."""
    from rest_framework.metadata import BaseMetadata
    from rest_framework.views import APIView

    class Stale(BaseMetadata):
        pass

    rebind_api_settings()
    monkeypatch.setattr(APIView, "metadata_class", Stale)
    findings = check_rest_framework_settings_in_force()

    assert _ids(findings) == [E001_SETTING_NOT_IN_FORCE]
    finding = findings[0]
    assert finding.level >= checks.ERROR
    # It names the key, the attribute and both values — a reader must not have
    # to go and diff two objects to learn what is wrong.
    assert "DEFAULT_METADATA_CLASS" in finding.msg
    assert "rest_framework.views.APIView.metadata_class" in finding.msg
    assert "Stale" in finding.msg


def test_check_reports_a_key_whose_import_string_does_not_resolve(
    monkeypatch, settings
):
    from rest_framework.settings import api_settings

    settings.REST_FRAMEWORK = dict(settings.REST_FRAMEWORK or {})
    settings.REST_FRAMEWORK["DEFAULT_METADATA_CLASS"] = "nowhere.at.all.Metadata"
    api_settings.reload()
    try:
        findings = check_rest_framework_settings_in_force()
        assert E002_SETTING_UNRESOLVABLE in _ids(findings)
        assert "nowhere.at.all.Metadata" in " ".join(f.msg for f in findings)
    finally:
        api_settings.reload()
        rebind_api_settings()


def test_check_warns_when_drf_source_cannot_be_read(monkeypatch):
    monkeypatch.setattr(
        "stapel_core.django.drf_rebind.imported_drf_modules",
        lambda: ["rest_framework.views"],
    )
    monkeypatch.setattr(
        "stapel_core.django.drf_rebind._module_source",
        lambda name: None,
    )
    findings = check_rest_framework_settings_in_force()
    assert W001_SOURCE_UNREADABLE in _ids(findings)
    assert [f for f in findings if f.id == W001_SOURCE_UNREADABLE][0].level < checks.ERROR


def test_check_is_registered_under_its_tag():
    """`manage.py check --tag stapel_drf_settings` must select exactly it."""
    from django.core.checks.registry import registry as check_registry

    assert "stapel_drf_settings" in check_registry.tags_available()
    selected = check_registry.get_checks(include_deployment_checks=False)
    assert check_rest_framework_settings_in_force in selected
