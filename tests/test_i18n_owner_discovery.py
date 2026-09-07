"""Catalog discovery follows the error registry's owners, not only INSTALLED_APPS.

The defect this pins: a library installed only as a client (its ``.client``
imported, its app never listed in INSTALLED_APPS) registers its error codes by
import side effect — so the codes are in the canon — while the
``translations/`` its wheel ships were invisible to ``load_app_catalogs``,
which walked INSTALLED_APPS and ``EXTRA_CATALOG_DIRS`` only. A host's coverage
gate then reported the keys untranslated although the installed wheel carried
them, and the only cure was a per-host ``EXTRA_CATALOG_DIRS`` line naming the
library — a patch for one host, for a class every host shares.
"""
import importlib
import json
import sys
import textwrap
from pathlib import Path

import pytest

from stapel_core.django.api.errors import (
    _GLOBAL_REGISTRY,
    _OWNER_REGISTRY,
    build_error_registry,
    error_owner,
    register_service_errors,
)
from stapel_core.i18n import (
    CatalogDirError,
    catalog_search_dirs,
    check_registry_catalog_pairing,
    error_owner_roots,
    load_app_catalogs,
    load_catalog_file,
    owner_catalog,
    owner_languages,
    owner_of_dir,
    resolve_catalog_dir,
)
from stapel_core.i18n.conf import i18n_settings

PKG = "stapel_test_client_only_lib"
CODES = {
    "error.409.clientonly.legal_hold": "Account is under legal hold",
    "error.404.clientonly.export_missing": "Export not found",
}
RU = {
    "error.409.clientonly.legal_hold": "Аккаунт под юридическим удержанием",
    "error.404.clientonly.export_missing": "Экспорт не найден",
    # A key core owns and ships in ru: an owner root must rank BELOW the
    # installed app that carries the real text.
    "error.404.not_found": "shadow from a client-only library",
}


def _forget(codes):
    for code in codes:
        _GLOBAL_REGISTRY.pop(code, None)
        _OWNER_REGISTRY.pop(code, None)


@pytest.fixture
def client_only_owner(tmp_path, monkeypatch):
    """A wheel-shaped package: registers codes on import, ships errors.ru.json."""
    site = tmp_path / "site"
    pkg = site / PKG
    (pkg / "translations").mkdir(parents=True)
    (pkg / "__init__.py").write_text(textwrap.dedent(f"""
        from stapel_core.django.api.errors import register_service_errors

        register_service_errors({CODES!r})
    """), encoding="utf-8")
    (pkg / "translations" / "errors.ru.json").write_text(
        json.dumps(RU, ensure_ascii=False), encoding="utf-8")
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    importlib.import_module(PKG)  # the client import: registration by side effect
    try:
        yield pkg
    finally:
        sys.modules.pop(PKG, None)
        _forget(CODES)


def _core_ru():
    import stapel_core.django as core_django

    return load_catalog_file(
        Path(core_django.__file__).parent / "translations" / "errors.ru.json")


def test_client_only_owner_catalog_is_discovered(client_only_owner, settings):
    assert PKG not in settings.INSTALLED_APPS
    assert error_owner("error.409.clientonly.legal_hold") == PKG

    merged = load_app_catalogs("errors", "ru")

    assert merged["error.409.clientonly.legal_hold"] == RU["error.409.clientonly.legal_hold"]
    assert merged["error.404.clientonly.export_missing"] == RU["error.404.clientonly.export_missing"]
    assert client_only_owner.resolve() in [d.resolve() for d in catalog_search_dirs()]


HOST_APP = "stapel_test_host_app"


def _catalog_root(root, key, text):
    (root / "translations").mkdir(parents=True)
    (root / "translations" / "errors.ru.json").write_text(
        json.dumps({key: text}, ensure_ascii=False), encoding="utf-8")
    return root


def test_owner_roots_rank_below_installed_apps_and_extra_dirs(
        client_only_owner, settings, tmp_path, monkeypatch):
    """Later-wins is untouched: owner < INSTALLED_APPS < EXTRA_CATALOG_DIRS."""
    key = "error.409.clientonly.legal_hold"
    app = _catalog_root(tmp_path / "apps" / HOST_APP, key, "installed app reword")
    (app / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "apps"))
    importlib.invalidate_caches()
    extra = _catalog_root(tmp_path / "config_repo", key, "host reword")

    settings.INSTALLED_APPS = [*settings.INSTALLED_APPS, HOST_APP]
    settings.STAPEL_I18N = {"EXTRA_CATALOG_DIRS": [str(extra)]}
    i18n_settings.reload()
    try:
        dirs = [d.resolve() for d in catalog_search_dirs()]
        assert len(dirs) == len(set(dirs))
        assert (dirs.index(client_only_owner.resolve())
                < dirs.index(app.resolve())
                < dirs.index(extra.resolve()))

        # An installed app outranks a client-only owner's text ...
        settings.STAPEL_I18N = {"EXTRA_CATALOG_DIRS": []}
        i18n_settings.reload()
        assert load_app_catalogs("errors", "ru")[key] == "installed app reword"
        # ... and the extra dir (the host) outranks both.
        settings.STAPEL_I18N = {"EXTRA_CATALOG_DIRS": [str(extra)]}
        i18n_settings.reload()
        assert load_app_catalogs("errors", "ru")[key] == "host reword"
        # An owner that IS an installed app keeps its INSTALLED_APPS slot.
        register_service_errors({"error.409.hostapp.probe": "p"}, owner=HOST_APP)
        try:
            dirs = [d.resolve() for d in catalog_search_dirs()]
            assert dirs.count(app.resolve()) == 1
            assert dirs.index(client_only_owner.resolve()) < dirs.index(app.resolve())
        finally:
            _forget(["error.409.hostapp.probe"])
    finally:
        sys.modules.pop(HOST_APP, None)
        i18n_settings.reload()


def test_ownership_resolves_over_owner_roots(client_only_owner):
    assert owner_of_dir(client_only_owner / "translations") == PKG
    assert owner_languages(PKG, "errors") == {"ru"}
    assert owner_catalog(PKG, "errors", "ru")["error.404.clientonly.export_missing"] == (
        RU["error.404.clientonly.export_missing"])
    # The pairing gate sees the same roots as the loader: the client-only
    # owner ships ru and carries every code it declares, so no `unshipped`.
    own = [e for e in build_error_registry() if e["owner"] == PKG]
    assert len(own) == 2
    assert check_registry_catalog_pairing(own) == []


def test_owner_without_a_package_dir_is_skipped_and_listed(tmp_path, monkeypatch):
    ns = tmp_path / "ns_site" / "stapel_test_namespace_owner"
    ns.mkdir(parents=True)  # no __init__.py: a namespace package
    monkeypatch.syspath_prepend(str(tmp_path / "ns_site"))
    importlib.invalidate_caches()
    ghosts = {
        "error.500.ghost.bare_module": "x",
        "error.500.ghost.unimportable": "y",
        "error.500.ghost.namespace": "z",
    }
    register_service_errors({"error.500.ghost.bare_module": "x"}, owner="hashlib")
    register_service_errors({"error.500.ghost.unimportable": "y"},
                            owner="stapel_test_no_such_package")
    register_service_errors({"error.500.ghost.namespace": "z"},
                            owner="stapel_test_namespace_owner")
    try:
        roots = error_owner_roots()
        assert roots["hashlib"] is None
        assert roots["stapel_test_no_such_package"] is None
        assert roots["stapel_test_namespace_owner"] is None
        assert roots["stapel_core"] is not None
        dirs = catalog_search_dirs()  # no raise
        assert ns.resolve() not in [d.resolve() for d in dirs]

        with pytest.raises(CatalogDirError) as exc:
            resolve_catalog_dir(tmp_path / "nowhere" / "translations", cwd=tmp_path)
        message = str(exc.value)
        assert "skipped: hashlib, stapel_test_namespace_owner, stapel_test_no_such_package" in message
    finally:
        _forget(ghosts)
