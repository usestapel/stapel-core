"""Embedded-library static discovery (stapel_core.staticfiles) and its check.

The defect this covers, in the shape it was found in: a library that is
IMPORTED rather than installed as a Django app — ``stapel_attributes`` is the
first, ``stapel_categories``' feature editor and ``stapel_forms``' admin
builder are the two callers — ships an admin bundle under ``static/``. It
never appears in ``INSTALLED_APPS``, so ``AppDirectoriesFinder`` does not walk
it and ``collectstatic`` never sees the file. The admin then renders, saves,
and silently offers no config editor.

The old ``get_staticfiles_dirs()`` knew two directories: the service's own and
``stapel_core``'s. Every third embedded library had to be named by hand, in
every host's settings module — which is to say the failure was one forgotten
line away at all times, on a page nobody rereads.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stapel_core.staticfiles import (
    embedded_static_dirs,
    embedded_static_packages,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    reset_cache()
    yield
    reset_cache()


def _make_package(root: Path, name: str, *, static_files=(), importable=False) -> Path:
    """A minimal package on disk; optionally shipping static/.

    ``importable=False`` (the default) makes the package EXPLODE on import, so
    that any accidental import during discovery is a loud test failure rather
    than a slow one.
    """
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "" if importable else "raise AssertionError('imported')\n"
    )
    for rel in static_files:
        target = pkg / "static" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("/* bundle */\n")
    return pkg


def _make_dist_info(root: Path, dist: str, version: str, top_level=None) -> Path:
    """A .dist-info directory of the shape pip leaves behind."""
    info = root / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Name: {dist}\nVersion: {version}\n")
    if top_level is not None:
        (info / "top_level.txt").write_text("\n".join(top_level) + "\n")
    return info


@pytest.fixture
def on_path(monkeypatch):
    """Prepend a directory to sys.path for the duration of one test."""

    def _add(directory: Path):
        monkeypatch.syspath_prepend(str(directory))
        reset_cache()

    return _add


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_finds_static_of_an_embedded_library(tmp_path, on_path):
    """The whole bug: a stapel_* package shipping static/ is found unaided."""
    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakeattrs", static_files=["stapel_fakeattrs/admin.js"])
    on_path(site)

    dirs = embedded_static_dirs()

    assert str(site / "stapel_fakeattrs" / "static") in dirs


def test_discovery_does_not_import_the_library(tmp_path, on_path):
    """Import cost at settings time is the reason this is a path walk.

    Every synthetic package here raises on import; the assertion is that
    discovery never pays that cost (nor a real library's Django imports,
    which at settings-module time would be a circular import).
    """
    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakeattrs", static_files=["stapel_fakeattrs/admin.js"])
    on_path(site)

    embedded_static_dirs()

    assert "stapel_fakeattrs" not in sys.modules


def test_package_without_static_is_not_reported(tmp_path, on_path):
    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakeplain")
    on_path(site)

    assert not any("stapel_fakeplain" in d for d in embedded_static_dirs())


def test_non_stapel_package_is_ignored(tmp_path, on_path):
    site = tmp_path / "site-packages"
    _make_package(site, "notstapel_fake", static_files=["notstapel_fake/admin.js"])
    on_path(site)

    assert not any("notstapel_fake" in d for d in embedded_static_dirs())


def test_result_is_sorted_and_deduplicated(tmp_path, on_path):
    """Deterministic output: same env, same list, whatever sys.path order."""
    site = tmp_path / "site-packages"
    for name in ("stapel_fakezed", "stapel_fakealpha", "stapel_fakemid"):
        _make_package(site, name, static_files=[f"{name}/admin.js"])
    on_path(site)
    on_path(site)  # the same directory twice

    dirs = [d for d in embedded_static_dirs() if "stapel_fake" in d]

    assert dirs == [
        str(site / "stapel_fakealpha" / "static"),
        str(site / "stapel_fakemid" / "static"),
        str(site / "stapel_fakezed" / "static"),
    ]


def test_dist_info_names_a_package_outside_sys_path(tmp_path, on_path):
    """The editable-install shape: metadata on sys.path, code elsewhere.

    ``pip install -e`` leaves a .dist-info in site-packages and puts the code
    behind an import hook, so the directory walk alone would miss it. The
    dist-info's top_level.txt is read (no metadata API, no import) and the
    package resolved through the normal finders.
    """
    site = tmp_path / "site-packages"
    site.mkdir()
    elsewhere = tmp_path / "checkout"
    _make_package(elsewhere, "stapel_fakeedit", static_files=["stapel_fakeedit/admin.js"])
    _make_dist_info(site, "stapel-fakeedit", "1.2.3", top_level=["stapel_fakeedit"])
    on_path(site)
    on_path(elsewhere)

    dirs = embedded_static_dirs()

    assert str(elsewhere / "stapel_fakeedit" / "static") in dirs


def test_dist_info_without_top_level_falls_back_to_the_dist_name(tmp_path, on_path):
    site = tmp_path / "site-packages"
    _make_package(site, "stapel_faketl", static_files=["stapel_faketl/admin.js"])
    _make_dist_info(site, "stapel-faketl", "0.1.0", top_level=None)
    on_path(site)

    assert str(site / "stapel_faketl" / "static") in embedded_static_dirs()


def test_packages_pairs_carry_the_package_name(tmp_path, on_path):
    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakepair", static_files=["stapel_fakepair/admin.js"])
    on_path(site)

    pairs = dict(embedded_static_packages())

    assert pairs["stapel_fakepair"] == str(site / "stapel_fakepair" / "static")


def test_core_own_static_is_discovered_without_being_named():
    """stapel_core ships static/admin/js — the mechanism must find its own.

    Located through the IMPORTED package, not through this file's parent: CI
    installs the wheel and runs the tests against site-packages, so a repo-root
    path would assert something true only of a working copy.
    """
    import os

    import stapel_core

    core_static = os.path.realpath(
        Path(stapel_core.__file__).resolve().parent / "static"
    )
    discovered = {os.path.realpath(d) for d in embedded_static_dirs()}

    assert core_static in discovered


# ---------------------------------------------------------------------------
# get_staticfiles_dirs()
# ---------------------------------------------------------------------------


def test_get_staticfiles_dirs_includes_embedded_libraries(tmp_path, on_path):
    from stapel_core.django.settings import get_staticfiles_dirs

    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakehost", static_files=["stapel_fakehost/admin.js"])
    on_path(site)

    dirs = get_staticfiles_dirs(tmp_path / "svc")

    assert str(site / "stapel_fakehost" / "static") in dirs


def test_get_staticfiles_dirs_puts_the_service_first(tmp_path, on_path):
    """Host assets must still win a name collision with a library's."""
    from stapel_core.django.settings import get_staticfiles_dirs

    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakeorder", static_files=["stapel_fakeorder/admin.js"])
    on_path(site)
    service = tmp_path / "svc"
    (service / "static").mkdir(parents=True)

    dirs = get_staticfiles_dirs(service)

    assert dirs[0] == str(service / "static")
    assert dirs.index(str(site / "stapel_fakeorder" / "static")) > 0


def test_get_staticfiles_dirs_does_not_list_core_static_twice():
    """COMMON_STATIC_DIR and the discovered stapel_core/static are one dir."""
    from stapel_core.django.settings import COMMON_STATIC_DIR, get_staticfiles_dirs

    dirs = get_staticfiles_dirs(Path("/nonexistent-service"))

    assert dirs.count(COMMON_STATIC_DIR) == 1


def test_get_staticfiles_dirs_can_be_asked_not_to_discover(tmp_path, on_path):
    from stapel_core.django.settings import get_staticfiles_dirs

    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakeoptout", static_files=["stapel_fakeoptout/admin.js"])
    on_path(site)

    dirs = get_staticfiles_dirs(tmp_path / "svc", include_embedded=False)

    assert not any("stapel_fakeoptout" in d for d in dirs)


# ---------------------------------------------------------------------------
# The check — stapel_core.static.W001
# ---------------------------------------------------------------------------


def test_check_fires_when_an_embedded_bundle_is_not_findable(tmp_path, on_path, settings):
    from stapel_core.django.static_checks import (
        W001_EMBEDDED_STATIC_UNREACHABLE,
        check_embedded_static_collectable,
    )

    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakemissing", static_files=["stapel_fakemissing/admin.js"])
    on_path(site)
    settings.STATICFILES_DIRS = []

    findings = [
        f for f in check_embedded_static_collectable() if "stapel_fakemissing" in f.msg
    ]

    assert [f.id for f in findings] == [W001_EMBEDDED_STATIC_UNREACHABLE]
    assert "stapel_fakemissing/admin.js" in findings[0].msg


def test_check_is_silent_once_the_directory_is_on_staticfiles_dirs(
    tmp_path, on_path, settings
):
    from stapel_core.django.static_checks import check_embedded_static_collectable

    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakeok", static_files=["stapel_fakeok/admin.js"])
    on_path(site)
    settings.STATICFILES_DIRS = [str(site / "stapel_fakeok" / "static")]

    assert not any("stapel_fakeok" in f.msg for f in check_embedded_static_collectable())


def test_check_is_silent_under_the_mechanism_it_guards(tmp_path, on_path, settings):
    """The end-to-end shape: get_staticfiles_dirs() alone clears the check."""
    from stapel_core.django.settings import get_staticfiles_dirs
    from stapel_core.django.static_checks import check_embedded_static_collectable

    site = tmp_path / "site-packages"
    _make_package(site, "stapel_fakewired", static_files=["stapel_fakewired/admin.js"])
    on_path(site)
    settings.STATICFILES_DIRS = get_staticfiles_dirs(tmp_path / "svc")

    assert check_embedded_static_collectable() == []


def test_check_skips_packages_django_already_walks(tmp_path, on_path, settings):
    """A real INSTALLED_APPS app is AppDirectoriesFinder's job, not ours."""
    from stapel_core.django.static_checks import check_embedded_static_collectable

    site = tmp_path / "site-packages"
    _make_package(
        site, "stapel_fakeapp", static_files=["stapel_fakeapp/admin.js"], importable=True
    )
    on_path(site)
    settings.STATICFILES_DIRS = []
    settings.INSTALLED_APPS = list(settings.INSTALLED_APPS) + ["stapel_fakeapp"]

    assert not any(
        "stapel_fakeapp" in f.msg for f in check_embedded_static_collectable()
    )


def test_check_reports_one_finding_per_package(tmp_path, on_path, settings):
    from stapel_core.django.static_checks import check_embedded_static_collectable

    site = tmp_path / "site-packages"
    _make_package(
        site,
        "stapel_fakemulti",
        static_files=[
            "stapel_fakemulti/admin.js",
            "stapel_fakemulti/admin.css",
            "stapel_fakemulti/locales/en.json",
        ],
    )
    on_path(site)
    settings.STATICFILES_DIRS = []

    findings = [
        f for f in check_embedded_static_collectable() if "stapel_fakemulti" in f.msg
    ]

    assert len(findings) == 1
    assert "3 static file(s)" in findings[0].msg  # one finding, every asset counted


def test_check_is_registered_with_django(tmp_path):
    """Registered on import of the app config, like every other core check."""
    from django.core.checks import registry

    from stapel_core.django.static_checks import check_embedded_static_collectable

    assert check_embedded_static_collectable in registry.registry.get_checks()


def test_locale_json_of_the_real_attributes_bundle_is_covered(tmp_path, on_path, settings):
    """The bundle is not one file: the editor's catalogs ship beside it.

    stapel-attributes' widget reads ``locales/*.json`` through the same
    finders, so a check that only looked for ``attributes-admin.js`` would
    pass a deployment whose editor renders untranslated.
    """
    from stapel_core.django.static_checks import check_embedded_static_collectable

    site = tmp_path / "site-packages"
    pkg = _make_package(site, "stapel_fakelocale", static_files=["stapel_fakelocale/admin.js"])
    locales = pkg / "static" / "stapel_fakelocale" / "locales"
    locales.mkdir(parents=True)
    (locales / "en.json").write_text(json.dumps({"a": "b"}))
    on_path(site)
    settings.STATICFILES_DIRS = []

    findings = [
        f for f in check_embedded_static_collectable() if "stapel_fakelocale" in f.msg
    ]

    assert "2 static file(s)" in findings[0].msg
