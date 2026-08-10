"""Ownership scoping: what a module must translate, and what it may not.

The defect this pins: ``source_texts("errors")`` is the whole in-process
registry, and the gate used to demand every canonical key from every module's
own ``translations/`` directory. Five libraries therefore shipped 410
byte-identical copies of core's 41 cross-cutting keys — not one of them an
intentional reword, all of them a future stale shadow of a text core owns.

Two mechanisms, tested here:

* the canon is scoped by owner, so a module answers for its own keys only;
* copying a key its owner already ships in that language is a ``foreign``
  error unless the sidecar declares the override — which is what tells an
  intentional reword apart from stale copy-paste.
"""
import pytest

from stapel_core.django.api.errors import (
    _GLOBAL_REGISTRY,
    _OWNER_REGISTRY,
    CORE_OWNER,
    error_owner,
    error_owners,
    register_service_errors,
)
from stapel_core.i18n import (
    STATE_FILENAME,
    StateSidecar,
    check_translation_catalogs,
    dump_catalog,
    module_catalog,
    owned_keys,
    translate_catalog,
)
from stapel_core.i18n.errordocs import build_error_docs

SOURCE = {
    "error.404.not_found": "Requested resource not found",
    "error.400.captcha_invalid": "Captcha verification failed. Please try again.",
    "error.409.profile_taken": "Profile already exists",
}
OWNERS = {
    "error.404.not_found": "stapel_core",
    "error.400.captcha_invalid": "stapel_core",
    "error.409.profile_taken": "stapel_profiles",
}
#: What core publishes in ru — the "does the owner ship this" fact.
CORE_RU = {
    "error.404.not_found": "Запрашиваемый ресурс не найден",
    "error.400.captcha_invalid": "Проверка капчи не пройдена. Попробуйте ещё раз.",
}


def _upstream(owner, domain, language):
    return CORE_RU if (owner == "stapel_core" and language == "ru") else {}


def _write(tmp_path, catalog, state_rows=None):
    out = tmp_path / "translations"
    out.mkdir(exist_ok=True)
    (out / "errors.ru.json").write_text(dump_catalog(catalog), encoding="utf-8")
    if state_rows:
        sidecar = StateSidecar(out / STATE_FILENAME)
        for key, row in state_rows.items():
            sidecar.set("errors", "ru", key, source_hash=row["hash"],
                        origin=row.get("origin", "human"))
            if row.get("override"):
                sidecar.declare_override("errors", "ru", key,
                                         owner=row["override"])
        sidecar.save()
    return out


def _check(out, **kw):
    return check_translation_catalogs(
        "errors", out,
        source_texts=SOURCE, languages=["ru"],
        owners=OWNERS, owner_catalogs=_upstream,
        **kw,
    )


# ---------------------------------------------------------------------------
# scoping — a module answers for its own keys
# ---------------------------------------------------------------------------

def test_owned_keys_scopes_the_canon_to_its_owner():
    assert set(owned_keys(SOURCE, OWNERS, "stapel_profiles")) == {
        "error.409.profile_taken"
    }
    assert set(owned_keys(SOURCE, OWNERS, "stapel_core")) == {
        "error.404.not_found", "error.400.captcha_invalid"
    }


def test_key_with_no_known_owner_stays_everyones_duty():
    """An unattributed key must still be translated — never silently dropped."""
    assert "error.500.orphan" in owned_keys(
        {**SOURCE, "error.500.orphan": "x"}, OWNERS, "stapel_profiles"
    )


def test_unscoped_when_ownership_does_not_resolve(tmp_path):
    """No owner (a tmp_path unit test) → exactly the pre-ownership behaviour."""
    out = _write(tmp_path, {})
    missing = [i for i in _check(out) if i.code == "missing"]
    assert len(missing) == len(SOURCE)


def test_module_no_longer_has_to_translate_core_keys(tmp_path):
    """The 410-duplicate root cause: coverage stops demanding foreign keys."""
    out = _write(
        tmp_path,
        {"error.409.profile_taken": "Профиль уже существует"},
        {"error.409.profile_taken": {"hash": _hash("Profile already exists")}},
    )
    issues = _check(out, owner="stapel_profiles")
    assert [i for i in issues if i.level == "error"] == []


# ---------------------------------------------------------------------------
# the refusal — a silent re-translation cannot pass
# ---------------------------------------------------------------------------

def test_copying_a_core_key_is_a_blocking_error(tmp_path):
    out = _write(tmp_path, {
        "error.409.profile_taken": "Профиль уже существует",
        "error.404.not_found": "Запрашиваемый ресурс не найден",
    }, {"error.409.profile_taken": {"hash": _hash("Profile already exists")}})
    foreign = [i for i in _check(out, owner="stapel_profiles") if i.code == "foreign"]
    assert len(foreign) == 1
    assert foreign[0].level == "error"
    assert "stapel_core" in foreign[0].message


def test_gap_filling_a_language_the_owner_does_not_ship_is_allowed(tmp_path):
    """A host generating a language for the whole fleet shadows nothing."""
    out = _write(tmp_path, {
        "error.409.profile_taken": "Профиль уже существует",
        "error.400.captcha_invalid": "Проверка капчи не пройдена",
    }, {"error.409.profile_taken": {"hash": _hash("Profile already exists")}})
    upstream_without_captcha = {
        "error.404.not_found": CORE_RU["error.404.not_found"]
    }
    issues = check_translation_catalogs(
        "errors", out,
        source_texts=SOURCE, languages=["ru"],
        owners=OWNERS, owner="stapel_profiles",
        owner_catalogs=lambda o, d, lg: upstream_without_captcha,
    )
    assert [i for i in issues if i.code == "foreign"] == []


def test_a_declared_reword_passes(tmp_path):
    out = _write(tmp_path, {
        "error.409.profile_taken": "Профиль уже существует",
        "error.404.not_found": "Такой страницы у нас нет",
    }, {
        "error.409.profile_taken": {"hash": _hash("Profile already exists")},
        "error.404.not_found": {
            "hash": _hash(SOURCE["error.404.not_found"]),
            "override": "stapel_core",
        },
    })
    issues = _check(out, owner="stapel_profiles")
    assert [i for i in issues if i.level == "error"] == []


def test_a_declaration_that_repeats_the_owners_text_is_flagged(tmp_path):
    out = _write(tmp_path, {
        "error.409.profile_taken": "Профиль уже существует",
        "error.404.not_found": CORE_RU["error.404.not_found"],
    }, {
        "error.409.profile_taken": {"hash": _hash("Profile already exists")},
        "error.404.not_found": {
            "hash": _hash(SOURCE["error.404.not_found"]),
            "override": "stapel_core",
        },
    })
    issues = _check(out, owner="stapel_profiles")
    assert [i.code for i in issues if i.level == "error"] == []
    assert any(i.code == "vacuous_override" for i in issues)


def test_undeclared_overrides_switch_downgrades_to_a_warning(tmp_path):
    """The host escape hatch — fleet libraries run the default (`error`)."""
    out = _write(tmp_path, {
        "error.409.profile_taken": "Профиль уже существует",
        "error.404.not_found": "Такой страницы у нас нет",
    }, {"error.409.profile_taken": {"hash": _hash("Profile already exists")}})
    issues = _check(out, owner="stapel_profiles", undeclared_overrides="warn")
    assert [i for i in issues if i.level == "error"] == []
    assert any(i.code == "foreign" and i.level == "warning" for i in issues)


# ---------------------------------------------------------------------------
# the write side refuses too — the command that made the duplicates
# ---------------------------------------------------------------------------

def test_translate_will_not_emit_a_key_the_package_does_not_own(tmp_path):
    out = tmp_path / "translations"
    out.mkdir()
    result = translate_catalog(
        "errors", "ru", out,
        source_texts=SOURCE, owner="stapel_profiles", owners=OWNERS,
        seed={k: f"[ru] {v}" for k, v in SOURCE.items()}, seed_label="corpus",
    )
    from stapel_core.i18n import load_catalog_file

    assert set(load_catalog_file(out / "errors.ru.json")) == {
        "error.409.profile_taken"
    }
    assert result.seeded == 1


def test_declaring_an_override_admits_the_key_and_records_the_owner(tmp_path):
    out = tmp_path / "translations"
    out.mkdir()
    translate_catalog(
        "errors", "ru", out,
        source_texts=SOURCE, owner="stapel_profiles", owners=OWNERS,
        declare_override=["error.404.not_found"],
        seed={k: f"[ru] {v}" for k, v in SOURCE.items()}, seed_label="corpus",
    )
    from stapel_core.i18n import load_catalog_file

    assert "error.404.not_found" in load_catalog_file(out / "errors.ru.json")
    sidecar = StateSidecar(out / STATE_FILENAME)
    assert sidecar.overrides("errors", "ru") == {
        "error.404.not_found": "stapel_core"
    }


def test_a_declaration_survives_retranslation(tmp_path):
    """`set` used to overwrite the row wholesale, dropping the declaration."""
    out = tmp_path / "translations"
    out.mkdir()
    sidecar = StateSidecar(out / STATE_FILENAME)
    sidecar.declare_override("errors", "ru", "error.404.not_found",
                             owner="stapel_core")
    sidecar.set("errors", "ru", "error.404.not_found",
                source_hash="deadbeef", origin="llm")
    assert sidecar.overrides("errors", "ru") == {
        "error.404.not_found": "stapel_core"
    }


def test_cannot_declare_an_override_of_a_key_you_own(tmp_path):
    out = tmp_path / "translations"
    out.mkdir()
    with pytest.raises(ValueError, match="owns it already"):
        translate_catalog(
            "errors", "ru", out,
            source_texts=SOURCE, owner="stapel_profiles", owners=OWNERS,
            declare_override=["error.409.profile_taken"],
        )


def test_cannot_declare_an_override_of_an_unowned_key(tmp_path):
    out = tmp_path / "translations"
    out.mkdir()
    with pytest.raises(ValueError, match="no package owns it"):
        translate_catalog(
            "errors", "ru", out,
            source_texts=SOURCE, owner="stapel_profiles", owners=OWNERS,
            declare_override=["error.500.nobodys"],
        )


# ---------------------------------------------------------------------------
# the reader half — pruning must not degrade what a reader sees
# ---------------------------------------------------------------------------
#
# Scoping the WRITE side without teaching the READ side is the same defect in a
# new costume: `build_error_docs` read the module's own translations/ directory
# and nothing else, so the moment a module dropped its copies of core's keys
# its Russian error reference lost those rows to `_(en)_` fallbacks — a silent
# downgrade to English with no gate to notice. Ownership resolves for readers
# too: a key the module does not own is read from its owner's catalog.

def test_module_catalog_reads_a_foreign_key_from_its_owner(tmp_path):
    out = _write(tmp_path, {"error.409.profile_taken": "Профиль уже существует"})
    resolved = module_catalog(
        "errors", "ru", out, keys=SOURCE,
        owner="stapel_profiles", owners=OWNERS, owner_catalogs=_upstream,
    )
    assert resolved["error.404.not_found"] == CORE_RU["error.404.not_found"]
    assert resolved["error.409.profile_taken"] == "Профиль уже существует"


def test_the_modules_own_text_wins_over_its_owners(tmp_path):
    """A declared reword is what the module ships — the runtime merges it last."""
    out = _write(tmp_path, {"error.404.not_found": "Такой страницы у нас нет"})
    resolved = module_catalog(
        "errors", "ru", out, keys=SOURCE,
        owner="stapel_profiles", owners=OWNERS, owner_catalogs=_upstream,
    )
    assert resolved["error.404.not_found"] == "Такой страницы у нас нет"


def test_a_key_the_module_owns_is_never_backfilled(tmp_path):
    """An owner's own gap is a coverage error — filling it would hide it."""
    out = _write(tmp_path, {})
    resolved = module_catalog(
        "errors", "ru", out, keys=SOURCE,
        owner="stapel_core", owners=OWNERS,
        owner_catalogs=lambda o, d, lg: CORE_RU,
    )
    assert "error.404.not_found" not in resolved


def test_pruning_leaves_the_error_reference_byte_identical(tmp_path):
    """The proof the sweep rests on, in Russian, not as an assertion of intent.

    Same registry, same owner catalog: the reference a module renders while it
    still duplicates core's keys and the one it renders after deleting them are
    the same bytes.
    """
    (tmp_path / "before").mkdir()
    (tmp_path / "after").mkdir()
    duplicated = _write(tmp_path / "before", dict(CORE_RU))
    pruned = _write(tmp_path / "after", {})
    kw = dict(owners=OWNERS, owner_catalogs=_upstream, source_language="en")
    before = build_error_docs("ru", translations_dir=duplicated, **kw)
    after = build_error_docs("ru", translations_dir=pruned, **kw)
    assert after == before
    row = next(ln for ln in after.splitlines()
               if ln.startswith("| `error.404.not_found`"))
    assert CORE_RU["error.404.not_found"] in row and "_(en)_" not in row


# ---------------------------------------------------------------------------
# the registry: who owns a key, and who cannot take it away
# ---------------------------------------------------------------------------

def test_core_owns_its_common_errors():
    assert error_owner("error.404.not_found") == CORE_OWNER
    assert error_owner("error.400.field.required") == CORE_OWNER


def test_re_registering_a_core_key_moves_the_text_but_not_the_ownership():
    """The §3 en-override seam still works — it just does not steal the duty."""
    key = "error.404.not_found"
    before = _GLOBAL_REGISTRY[key]
    try:
        register_service_errors({key: "Not found."})
        assert _GLOBAL_REGISTRY[key] == "Not found."
        assert error_owner(key) == CORE_OWNER
    finally:
        _GLOBAL_REGISTRY[key] = before


def test_first_registrant_owns_an_unclaimed_key():
    key = "error.418.ownership_probe"
    try:
        register_service_errors({key: "teapot"})
        # inferred from this test module's top-level package
        assert error_owner(key) == __name__.split(".")[0]
        register_service_errors({key: "still a teapot"})
        assert error_owner(key) == __name__.split(".")[0]
    finally:
        _GLOBAL_REGISTRY.pop(key, None)
        _OWNER_REGISTRY.pop(key, None)


def test_an_explicit_owner_claims_a_key_outright():
    key = "error.418.explicit_probe"
    try:
        register_service_errors({key: "teapot"})
        register_service_errors({key: "teapot"}, owner="stapel_core")
        assert error_owners()[key] == "stapel_core"
    finally:
        _GLOBAL_REGISTRY.pop(key, None)
        _OWNER_REGISTRY.pop(key, None)


def _hash(text):
    from stapel_core.i18n import content_hash

    return content_hash(text)
