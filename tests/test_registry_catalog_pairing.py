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

from stapel_core.i18n import (
    check_registry_catalog_pairing,
    check_translation_catalogs,
    dump_catalog,
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
