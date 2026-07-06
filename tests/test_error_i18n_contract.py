"""Error-i18n contracts (i18n-shipping.md §3/§4).

Pins the fork-free override seam of ``register_service_errors`` (a later
registration overrides an earlier en text — used by a host to re-word our
errors without a fork; must never be "fixed" into a duplicate-check), the
params-preservation gate over a localized override, and the byte-stable
``docs/errors.<lang>.md`` reference.
"""
from stapel_core.django.api.errors import (
    build_error_registry,
    register_service_errors,
)
from stapel_core.i18n import check_translation_catalogs, dump_catalog
from stapel_core.i18n.errordocs import build_error_docs, render_error_docs


def _entry(code):
    return {e["code"]: e for e in build_error_registry()}[code]


# ---------------------------------------------------------------------------
# §3 — later register_service_errors overrides en (the fork-free override seam)
# ---------------------------------------------------------------------------

def test_late_registration_overrides_en_text():
    key = "error.423.locked"  # a COMMON_ERRORS baseline key
    assert _entry(key)["en"] == "Resource is locked"
    # A host app's errors module (autodiscovered after ours) re-words it.
    register_service_errors({key: "This account is temporarily locked."})
    assert _entry(key)["en"] == "This account is temporarily locked."


def test_override_seam_is_last_wins_not_first_wins():
    key = "error.999.override_probe"
    register_service_errors({key: "first"})
    register_service_errors({key: "second"})
    assert _entry(key)["en"] == "second"


# ---------------------------------------------------------------------------
# §3/§5 — a localized override MUST preserve the canon {placeholders}
# ---------------------------------------------------------------------------

def test_locale_override_dropping_a_placeholder_fails_the_gate(tmp_path):
    key = "error.429.probe_rate_limit"
    register_service_errors({key: "Retry in {retry_after_minutes} minutes."})
    source = {key: _entry(key)["en"]}
    # a ru override that keeps the placeholder → clean
    (tmp_path / "errors.ru.json").write_text(
        dump_catalog({key: "Повторите через {retry_after_minutes} минут."}),
        encoding="utf-8")
    ok = check_translation_catalogs("errors", tmp_path, source_texts=source,
                                    languages=["ru"])
    assert not [i for i in ok if i.code == "params"]
    # a ru override that drops it → params error
    (tmp_path / "errors.ru.json").write_text(
        dump_catalog({key: "Повторите позже."}), encoding="utf-8")
    bad = check_translation_catalogs("errors", tmp_path, source_texts=source,
                                     languages=["ru"])
    assert [i for i in bad if i.code == "params" and i.level == "error"]


# ---------------------------------------------------------------------------
# §4 — docs/errors.<lang>.md reference
# ---------------------------------------------------------------------------

def test_error_docs_en_table_byte_stable_and_uses_registry():
    a = build_error_docs("en")
    b = build_error_docs("en")
    assert a == b and a.endswith("\n")
    assert a.startswith("# Errors — English")
    assert "| `error.400.bad_request` |" in a
    assert "Bad request" in a


def test_error_docs_localized_marks_uncovered_keys_as_en_fallback():
    entries = [
        {"code": "error.404.not_found", "status": 404, "params": [],
         "remediation": "retry", "en": "Requested resource not found"},
        {"code": "error.429.rate_limit", "status": 429,
         "params": ["retry_after_minutes"], "remediation": "wait_and_retry",
         "en": "Try again in {retry_after_minutes} minutes."},
    ]
    md = render_error_docs(entries, "ru",
                           catalog={"error.404.not_found": "Ресурс не найден"})
    assert "# Errors — Русский" in md
    assert "Ресурс не найден" in md            # covered
    assert "_(en)_" in md                       # uncovered key marked
    assert "`retry_after_minutes`" in md        # params rendered


# ---------------------------------------------------------------------------
# Management-command wiring (handlers invoked directly — the stapel_core.django
# app is not installed in the core test config, as with generate_error_keys).
# ---------------------------------------------------------------------------

def test_translate_and_check_commands_wire_end_to_end(tmp_path):
    import pytest

    from stapel_core.django.management.commands.check_translation_catalogs import (
        Command as Check,
    )
    from stapel_core.django.management.commands.generate_error_docs import (
        Command as Docs,
    )
    from stapel_core.django.management.commands.translate_catalogs import (
        Command as Translate,
    )

    # Seed a couple of real registry keys; the rest stay missing.
    seed = tmp_path / "seed.json"
    seed.write_text('{"error.400.bad_request": "Некорректный запрос", '
                    '"error.404.not_found": "Ресурс не найден"}', encoding="utf-8")
    Translate().handle(domain="errors", lang="ru", out=str(tmp_path),
                       seed=str(seed), seed_label="stapel-builtin", llm=False,
                       approve=None, approve_all=False)
    assert (tmp_path / "errors.ru.json").is_file()
    assert (tmp_path / ".state.json").is_file()

    # The gate fails (most keys still missing) → SystemExit(1).
    with pytest.raises(SystemExit):
        Check().handle(domain="errors", out=str(tmp_path), languages="ru", strict=False)

    # errors.en.md renders from the registry.
    Docs().handle(out=str(tmp_path), lang="en", translations=str(tmp_path))
    assert (tmp_path / "errors.en.md").read_text().startswith("# Errors — English")
