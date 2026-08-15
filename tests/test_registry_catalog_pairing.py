"""The two halves of the error-i18n contract cannot drift apart silently.

The contract: a package's registry export (``docs/errors.json``) declares
which codes exist; its ``translations/errors.<lang>.json`` say how they read.
The two are produced by different commands with different scopes — the
registry is instance-scoped (a deployment's codes), the catalogs are
ownership-scoped (a package's keys) — and the seam between them is where ten
gdpr strings silently fell back to English in a Russian UI, and where core's
41 translated keys reached no consumer at all.

Two gates close the seam from both sides:

* :func:`check_registry_catalog_pairing` — run by ``generate_error_keys``
  before writing: a declared code whose owner ships a language must be in
  that language's catalog, or emission refuses;
* the ``no_registry_export`` / ``unexported`` errors in
  :func:`check_translation_catalogs` — a package shipping catalogs for keys
  it owns must publish a registry export that declares them.
"""
import json
import sys
import types

from django.test import override_settings
from stapel_core.i18n import (
    check_registry_catalog_pairing,
    check_translation_catalogs,
    dump_catalog,
    export_codes,
    summarize,
)


def _entry(code, owner, en="Boom"):
    status = int(code.split(".")[1])
    return {"code": code, "status": status, "params": [],
            "remediation": "retry", "en": en, "owner": owner}


# ---------------------------------------------------------------------------
# check_registry_catalog_pairing (registry → catalogs, at emission)
# ---------------------------------------------------------------------------

def test_pairing_green_when_owner_catalogs_carry_every_code():
    entries = [_entry("error.409.gdpr.legal_hold", "stapel_gdpr")]
    issues = check_registry_catalog_pairing(
        entries,
        languages_of=lambda pkg, domain: {"ru"},
        owner_catalogs=lambda pkg, domain, lang: {
            "error.409.gdpr.legal_hold": "Аккаунт под юридическим удержанием.",
        },
    )
    assert issues == []


def test_pairing_red_when_a_shipped_language_lost_a_code():
    # The auth/gdpr incident shape: the registry declares the code, the owner
    # ships ru, and ru does not carry the key.
    entries = [
        _entry("error.409.gdpr.legal_hold", "stapel_gdpr"),
        _entry("error.410.gdpr.download_expired", "stapel_gdpr"),
    ]
    issues = check_registry_catalog_pairing(
        entries,
        languages_of=lambda pkg, domain: {"ru"},
        owner_catalogs=lambda pkg, domain, lang: {
            "error.409.gdpr.legal_hold": "Аккаунт под юридическим удержанием.",
        },
    )
    errors = [i for i in issues if i.level == "error"]
    assert len(errors) == 1
    assert errors[0].code == "untranslated"
    assert "error.410.gdpr.download_expired" in errors[0].message
    assert summarize(issues)[0] == 1


def test_pairing_warns_for_an_owner_that_ships_nothing_in_a_translated_fleet():
    entries = [
        _entry("error.404.video_not_found", "stapel_video"),
        _entry("error.404.not_found", "stapel_core"),
    ]
    issues = check_registry_catalog_pairing(
        entries,
        languages_of=lambda pkg, domain: {"ru"} if pkg == "stapel_core" else set(),
        owner_catalogs=lambda pkg, domain, lang: {"error.404.not_found": "Не найдено"},
    )
    assert [i.code for i in issues if i.level == "error"] == []
    warned = [i for i in issues if i.code == "unshipped"]
    assert len(warned) == 1 and "stapel_video" in warned[0].message


def test_pairing_silent_when_nobody_ships_catalogs():
    # An untranslated deployment has no translation contract to break.
    entries = [_entry("error.404.video_not_found", "stapel_video")]
    issues = check_registry_catalog_pairing(
        entries,
        languages_of=lambda pkg, domain: set(),
        owner_catalogs=lambda pkg, domain, lang: {},
    )
    assert issues == []


def test_pairing_skips_unowned_codes():
    issues = check_registry_catalog_pairing(
        [_entry("error.500.mystery", None)],
        languages_of=lambda pkg, domain: {"ru"},
        owner_catalogs=lambda pkg, domain, lang: {},
    )
    assert issues == []


# ---------------------------------------------------------------------------
# check_translation_catalogs (catalogs → registry, in the owner's gate)
# ---------------------------------------------------------------------------

def _write_catalog(tmp_path, lang, mapping):
    (tmp_path / f"errors.{lang}.json").write_text(
        dump_catalog(mapping), encoding="utf-8")


def test_catalogs_without_a_registry_export_go_red(tmp_path):
    source = {"error.409.gdpr.legal_hold": "Account is under a legal hold"}
    _write_catalog(tmp_path, "ru", {
        "error.409.gdpr.legal_hold": "Аккаунт под юридическим удержанием."})
    issues = check_translation_catalogs(
        "errors", tmp_path,
        source_texts=source,
        languages=["ru"],
        owner="stapel_gdpr",
        owners={"error.409.gdpr.legal_hold": "stapel_gdpr"},
        export_resolver=lambda owner: None,  # ships no docs/errors.json
    )
    red = [i for i in issues if i.code == "no_registry_export"]
    assert len(red) == 1 and red[0].level == "error"


def test_stale_registry_export_missing_a_translated_key_goes_red(tmp_path):
    source = {
        "error.409.gdpr.legal_hold": "Account is under a legal hold",
        "error.425.gdpr.export_not_ready": "Export is not ready yet",
    }
    owners = {k: "stapel_gdpr" for k in source}
    _write_catalog(tmp_path, "ru", {
        "error.409.gdpr.legal_hold": "Аккаунт под юридическим удержанием.",
        "error.425.gdpr.export_not_ready": "Экспорт ещё не готов.",
    })
    issues = check_translation_catalogs(
        "errors", tmp_path,
        source_texts=source,
        languages=["ru"],
        owner="stapel_gdpr",
        owners=owners,
        export_resolver=lambda owner: {"error.409.gdpr.legal_hold"},
    )
    red = [i for i in issues if i.code == "unexported"]
    assert len(red) == 1 and red[0].level == "error"
    assert "error.425.gdpr.export_not_ready" in red[0].message


def test_complete_registry_export_is_green(tmp_path):
    source = {"error.409.gdpr.legal_hold": "Account is under a legal hold"}
    _write_catalog(tmp_path, "ru", {
        "error.409.gdpr.legal_hold": "Аккаунт под юридическим удержанием."})
    issues = check_translation_catalogs(
        "errors", tmp_path,
        source_texts=source,
        languages=["ru"],
        owner="stapel_gdpr",
        owners={"error.409.gdpr.legal_hold": "stapel_gdpr"},
        export_resolver=lambda owner: {"error.409.gdpr.legal_hold"},
    )
    assert [i for i in issues
            if i.code in ("no_registry_export", "unexported")] == []


def test_ownerless_dir_skips_the_export_gate(tmp_path):
    # A tmp_path unit test (no resolvable owner) keeps the pre-ownership
    # behaviour — the export gate must not fire where ownership is unknown.
    source = {"error.400.probe": "Probe"}
    _write_catalog(tmp_path, "ru", {"error.400.probe": "Проба"})
    issues = check_translation_catalogs(
        "errors", tmp_path,
        source_texts=source,
        languages=["ru"],
        export_resolver=lambda owner: None,
    )
    assert [i for i in issues if i.code == "no_registry_export"] == []


# ---------------------------------------------------------------------------
# Where the export lives: inside the wheel for a distributable, in the project
# for a project's own app (a monolith has no wheel to put a docs/ inside).
# ---------------------------------------------------------------------------

def _project_app(tmp_path, package, *, export, package_export=None):
    """A project root, an app package inside it, and the committed export(s)."""
    root = tmp_path / "backend"
    pkg = root / package
    (pkg / "translations").mkdir(parents=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "errors.json").write_text(
        json.dumps(export), encoding="utf-8")
    if package_export is not None:
        (pkg / "docs").mkdir(parents=True)
        (pkg / "docs" / "errors.json").write_text(
            json.dumps(package_export), encoding="utf-8")
    return root, pkg


def _importable(monkeypatch, name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    monkeypatch.setitem(sys.modules, name, module)


def _installed_distribution(monkeypatch, root, package):
    """Make *package* look like what `pip install` leaves on disk: a dist-info."""
    dist = root / f"{package}-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {package}\nVersion: 1.0\n", encoding="utf-8")
    (dist / "top_level.txt").write_text(f"{package}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))


def test_a_project_app_is_declared_by_the_projects_export(tmp_path, monkeypatch):
    # An app inside a monolith ships nowhere and has no docs/ of its own; the
    # project's export is where its codes are declared.
    code = "error.404.room_not_found"
    root, pkg = _project_app(tmp_path, "rooms_app", export=[
        {"code": code, "status": 404, "en": "Room not found", "owner": "rooms_app"},
    ])
    _importable(monkeypatch, "rooms_app", pkg)
    _write_catalog(pkg / "translations", "ru", {code: "Комната не найдена"})
    with override_settings(BASE_DIR=str(root)):
        issues = check_translation_catalogs(
            "errors", pkg / "translations",
            source_texts={code: "Room not found"},
            languages=["ru"],
            owner="rooms_app",
            owners={code: "rooms_app"},
        )
    assert [i for i in issues
            if i.code in ("no_registry_export", "unexported")] == []


def test_a_project_export_elsewhere_is_named_by_the_setting(tmp_path, monkeypatch):
    code = "error.404.room_not_found"
    root, pkg = _project_app(tmp_path, "rooms_app", export=[])
    moved = root / "artifacts" / "errors.json"
    moved.parent.mkdir()
    moved.write_text(json.dumps(
        [{"code": code, "en": "Room not found", "owner": "rooms_app"}]),
        encoding="utf-8")
    _importable(monkeypatch, "rooms_app", pkg)
    with override_settings(BASE_DIR=str(root),
                           STAPEL_I18N={"REGISTRY_EXPORT": str(moved)}):
        assert export_codes("errors", "rooms_app") == {code}


def test_the_project_export_does_not_cover_an_installed_distribution(
        tmp_path, monkeypatch):
    # The failure mode the gate exists for: a LIBRARY ships catalogs and no
    # registry export. It is installed inside the project (a venv lives there
    # too), and the project's export even declares its code — none of that
    # substitutes for the artifact its own wheel must carry.
    code = "error.409.gdpr.legal_hold"
    root, pkg = _project_app(tmp_path, "gdpr_lib", export=[
        {"code": code, "status": 409, "en": "Legal hold", "owner": "gdpr_lib"},
    ])
    _installed_distribution(monkeypatch, root, "gdpr_lib")
    _importable(monkeypatch, "gdpr_lib", pkg)
    _write_catalog(pkg / "translations", "ru", {code: "Юридическое удержание."})
    with override_settings(BASE_DIR=str(root)):
        issues = check_translation_catalogs(
            "errors", pkg / "translations",
            source_texts={code: "Legal hold"},
            languages=["ru"],
            owner="gdpr_lib",
            owners={code: "gdpr_lib"},
        )
    red = [i for i in issues if i.code == "no_registry_export"]
    assert len(red) == 1 and red[0].level == "error"


def test_the_project_export_cannot_vouch_for_another_packages_key(
        tmp_path, monkeypatch):
    # No laundering: the project export answers for the codes it attributes to
    # this app, not for a neighbour's.
    code = "error.409.gdpr.legal_hold"
    root, pkg = _project_app(tmp_path, "rooms_app", export=[
        {"code": code, "status": 409, "en": "Legal hold", "owner": "stapel_gdpr"},
    ])
    _importable(monkeypatch, "rooms_app", pkg)
    _write_catalog(pkg / "translations", "ru", {code: "Юридическое удержание."})
    with override_settings(BASE_DIR=str(root)):
        assert export_codes("errors", "rooms_app") == set()
        issues = check_translation_catalogs(
            "errors", pkg / "translations",
            source_texts={code: "Legal hold"},
            languages=["ru"],
            owner="rooms_app",
            owners={code: "rooms_app"},
        )
    red = [i for i in issues if i.code == "unexported"]
    assert len(red) == 1 and code in red[0].message


def test_an_export_without_owners_still_declares_its_codes(tmp_path, monkeypatch):
    # A pre-`owner` artifact attributes nothing; an un-attributed entry counts,
    # exactly as an un-attributed key counts as owned in `owned_keys`.
    code = "error.404.room_not_found"
    root, pkg = _project_app(tmp_path, "rooms_app", export=[
        {"code": code, "status": 404, "en": "Room not found"},
    ])
    _importable(monkeypatch, "rooms_app", pkg)
    with override_settings(BASE_DIR=str(root)):
        assert export_codes("errors", "rooms_app") == {code}


def test_the_packages_own_export_wins_over_the_projects(tmp_path, monkeypatch):
    root, pkg = _project_app(
        tmp_path, "rooms_app",
        export=[{"code": "error.404.project", "en": "P", "owner": "rooms_app"}],
        package_export=[{"code": "error.404.package", "en": "K"}],
    )
    _importable(monkeypatch, "rooms_app", pkg)
    with override_settings(BASE_DIR=str(root)):
        assert export_codes("errors", "rooms_app") == {"error.404.package"}


def test_a_package_outside_the_project_gets_no_project_export(tmp_path, monkeypatch):
    root, _pkg = _project_app(tmp_path, "rooms_app", export=[
        {"code": "error.404.room_not_found", "en": "Room not found",
         "owner": "stray_pkg"},
    ])
    outside = tmp_path / "elsewhere" / "stray_pkg"
    outside.mkdir(parents=True)
    _importable(monkeypatch, "stray_pkg", outside)
    with override_settings(BASE_DIR=str(root)):
        assert export_codes("errors", "stray_pkg") is None


def test_emitted_artifact_json_carries_owner_field(tmp_path):
    # The registry projection is self-describing: every entry names the
    # package whose catalogs must carry its translations.
    from stapel_core.django.management.commands.generate_error_keys import Command

    out = tmp_path / "errors.json"
    Command().handle(out=str(out))
    entries = json.loads(out.read_text())
    assert all("owner" in e for e in entries)
    assert {e["owner"] for e in entries if e["code"] in (
        "error.404.not_found", "error.403.verification_required",
    )} == {"stapel_core"}
