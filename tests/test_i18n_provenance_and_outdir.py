"""Two i18n defects that both answered "done" while doing nothing.

Every test asserts BOTH directions: the defect is reproduced by an assertion
that fails on the old behaviour, and the legitimate look-alike stays silent.
The two bugs share a shape — a machine reporting success for work that did not
happen — so the tests are written to make the success itself falsifiable.

1. ``is_reviewed`` laundered machine output. Anything not ``origin: llm``
   counted as reviewed, so a translation routed through ``--seed`` (the cheap,
   obvious path) drove the gate's ``unreviewed`` counter to zero for text no
   human had read. It was found because an agent adding Spanish hand-populated
   the machinery's own ``.llm-cache.json`` to keep the provenance honest: the
   honest path was the non-obvious one.

2. ``translate_catalogs --out`` defaulted to ``translations`` relative to the
   working directory, while the loader walks the *package* directories of
   INSTALLED_APPS. Run from a service root the command wrote a file, printed
   success, and the catalog was never found.
"""
import io
import json
from pathlib import Path

import pytest

from stapel_core.i18n import (
    ORIGIN_HUMAN,
    ORIGIN_IMPORTED,
    ORIGIN_LLM,
    CatalogDirError,
    StateSidecar,
    catalog_search_dirs,
    check_translation_catalogs,
    content_hash,
    dump_catalog,
    is_curated,
    is_reviewed,
    is_seeded,
    load_app_catalogs,
    resolve_catalog_dir,
    seed_origin,
    translate_catalog,
)

SOURCE = {
    "error.400.bad_request": "Bad request",
    "error.404.not_found": "Requested resource not found",
}


class FakeTranslator:
    def translate(self, entries, source_language, target_language):
        return {k: f"[{target_language}] {v}" for k, v in entries.items()}


def _unreviewed(out, source=SOURCE, languages=("ru",)):
    issues = check_translation_catalogs(
        "errors", out, source_texts=source, languages=list(languages))
    return [i for i in issues if i.code == "unreviewed"]


# ---------------------------------------------------------------------------
# Defect 1 — provenance vocabulary: curated is not the same as reviewed
# ---------------------------------------------------------------------------

def test_seeded_values_are_not_reviewed():
    """The defect, at the predicate. ``is_reviewed("seed:x")`` was True."""
    assert is_reviewed(ORIGIN_HUMAN) is True
    assert is_reviewed(seed_origin("stapel-builtin")) is False
    assert is_reviewed(ORIGIN_LLM) is False
    assert is_reviewed(ORIGIN_IMPORTED) is False
    assert is_reviewed(None) is False


def test_curated_is_the_other_axis_and_keeps_seeds_from_being_overwritten():
    """Two facts, not one.

    ``is_curated`` answers "was this placed deliberately" — a seed qualifies,
    so it is never silently re-derived. ``is_reviewed`` answers "did a human
    read it" — a seed does not qualify. Collapsing the two is the defect; so
    would be collapsing them the other way and re-seeding stale corpus values.
    """
    assert is_curated(seed_origin("stapel-builtin")) is True
    assert is_curated(ORIGIN_HUMAN) is True
    assert is_curated(ORIGIN_IMPORTED) is True
    assert is_curated(ORIGIN_LLM) is False
    assert is_curated(None) is False
    assert is_seeded(seed_origin("x")) and not is_seeded(ORIGIN_LLM)


def test_seeding_the_whole_catalog_leaves_the_counter_honest(tmp_path):
    """The reported bug end to end.

    A full ``--seed`` pass used to leave zero unreviewed warnings — a gate
    reading green over text nobody had opened.
    """
    seed = {k: f"ru:{v}" for k, v in SOURCE.items()}
    result = translate_catalog("errors", "ru", tmp_path,
                               source_texts=SOURCE, seed=seed,
                               seed_label="stapel-builtin")
    assert result.seeded == 2 and not result.missing
    assert result.unreviewed == 2  # was 0: seeded counted as reviewed

    warnings = _unreviewed(tmp_path)
    assert len(warnings) == 2
    assert all(w.level == "warning" for w in warnings)
    assert "seed:stapel-builtin" in warnings[0].message


def test_approval_is_still_the_thing_that_clears_the_counter(tmp_path):
    """The legitimate case stays silent — otherwise the counter is just noise."""
    seed = {k: f"ru:{v}" for k, v in SOURCE.items()}
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, seed=seed)
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, approve_all=True)
    assert _unreviewed(tmp_path) == []
    st = StateSidecar(tmp_path / ".state.json")
    assert st.get("errors", "ru", "error.400.bad_request")["origin"] == ORIGIN_HUMAN


def test_seeded_values_are_not_the_gate_s_business_otherwise(tmp_path):
    """Re-classifying provenance must not invent ERRORS.

    The five shipped ``errors.ru.json`` catalogues are ~90% ``seed:*``. They
    keep their sidecars byte-for-byte; what changes is the W-counter, which is
    non-blocking. A change that turned them red would be a migration, not a fix.
    """
    seed = {k: f"ru:{v}" for k, v in SOURCE.items()}
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, seed=seed)
    issues = check_translation_catalogs(
        "errors", tmp_path, source_texts=SOURCE, languages=["ru"])
    assert [i for i in issues if i.level == "error"] == []


def test_a_stale_seed_is_still_never_silently_re_seeded(tmp_path):
    """The behaviour ``is_curated`` exists to preserve.

    The corpus was curated against the OLD en text. Re-seeding a key whose
    source moved would overwrite the value AND refresh its hash — hiding the
    very drift the ``stale`` error exists to report.
    """
    seed = {k: f"ru:{v}" for k, v in SOURCE.items()}
    translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE, seed=seed)
    edited = dict(SOURCE, **{"error.400.bad_request": "Bad request (v2)"})

    r = translate_catalog("errors", "ru", tmp_path, source_texts=edited, seed=seed)
    assert r.seeded == 0 and r.kept == 1
    stale = [i for i in check_translation_catalogs(
        "errors", tmp_path, source_texts=edited, languages=["ru"]) if i.code == "stale"]
    assert len(stale) == 1 and stale[0].level == "error"


def test_an_unattributed_catalog_is_imported_not_declared_human(tmp_path):
    """Onboarding a catalog file that has no sidecar.

    A hand-written catalog and a machine dump somebody committed are the same
    bytes on disk. Recording ``human`` there was the same laundering in another
    place; ``imported`` says exactly what is known — the value is protected
    from re-derivation and still counts as unreviewed.
    """
    (tmp_path / "errors.ru.json").write_text(
        dump_catalog({k: f"ru:{v}" for k, v in SOURCE.items()}), encoding="utf-8")

    r = translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE)
    assert r.imported == 2 and r.unreviewed == 2
    st = StateSidecar(tmp_path / ".state.json")
    assert st.get("errors", "ru", "error.400.bad_request")["origin"] == ORIGIN_IMPORTED
    assert len(_unreviewed(tmp_path)) == 2


def test_llm_output_is_still_unreviewed_and_the_run_reports_it(tmp_path):
    """The one case the old predicate got right must not regress."""
    r = translate_catalog("errors", "ru", tmp_path, source_texts=SOURCE,
                          llm=True, translator=FakeTranslator())
    assert r.translated == 2 and r.unreviewed == 2
    assert len(_unreviewed(tmp_path)) == 2


def test_existing_sidecars_keep_their_recorded_origin(tmp_path):
    """The migration, asserted.

    A shipped ``.state.json`` written before this change is read as-is: no
    rewrite, no re-approval, no re-generation. Only the interpretation of
    ``seed:*`` moves — from "reviewed" to "unreviewed", where it belongs.
    """
    (tmp_path / "errors.ru.json").write_text(
        dump_catalog({k: f"ru:{v}" for k, v in SOURCE.items()}), encoding="utf-8")
    (tmp_path / ".state.json").write_text(json.dumps({
        "errors.ru": {
            "error.400.bad_request": {
                "hash": content_hash(SOURCE["error.400.bad_request"]),
                "origin": "seed:stapel-builtin",
            },
        },
    }), encoding="utf-8")
    before = (tmp_path / ".state.json").read_text(encoding="utf-8")

    warnings = _unreviewed(tmp_path)
    assert {w.message.split("'")[1] for w in warnings} == set(SOURCE)
    assert (tmp_path / ".state.json").read_text(encoding="utf-8") == before
    assert StateSidecar(tmp_path / ".state.json").get(
        "errors", "ru", "error.400.bad_request")["origin"] == "seed:stapel-builtin"


# ---------------------------------------------------------------------------
# Defect 2 — the write target must be a directory the loader reads
# ---------------------------------------------------------------------------

def _app_root(tmp_path, name="app_a"):
    root = tmp_path / name
    (root / "translations").mkdir(parents=True)
    return root


def test_relative_out_from_a_service_root_is_refused(tmp_path):
    """The defect. ``--out translations`` from a service root resolved to
    ``<service>/translations`` — which ``load_app_catalogs`` never opens, since
    it walks app PACKAGE directories. The command reported success forever."""
    app = _app_root(tmp_path)
    service_root = tmp_path / "svc"
    service_root.mkdir()

    with pytest.raises(CatalogDirError) as exc:
        resolve_catalog_dir("translations", roots=[app], cwd=service_root)
    assert "never be found" in str(exc.value)
    assert str(app / "translations") in str(exc.value)  # says where to write


def test_relative_out_inside_an_app_package_is_accepted(tmp_path):
    """The legitimate case — a library repo whose root IS the app package.

    This is how every shipped module regenerates its catalog, so refusing it
    would trade a silent no-op for a loud one.
    """
    app = _app_root(tmp_path)
    assert resolve_catalog_dir("translations", roots=[app], cwd=app) == app / "translations"


def test_the_default_resolves_from_the_app_package_not_the_cwd(tmp_path):
    app = _app_root(tmp_path)
    nested = app / "sub" / "deeper"
    nested.mkdir(parents=True)
    # run from anywhere inside the package: the nearest enclosing app wins
    assert resolve_catalog_dir(None, roots=[app], cwd=nested) == app / "translations"


def test_the_default_refuses_outside_any_app_package(tmp_path):
    app = _app_root(tmp_path)
    with pytest.raises(CatalogDirError) as exc:
        resolve_catalog_dir(None, roots=[app], cwd=tmp_path)
    assert "--app" in str(exc.value)


def test_the_innermost_app_package_wins(tmp_path):
    """A nested app inside another app's tree must not write to the outer one."""
    outer = _app_root(tmp_path, "outer")
    inner = _app_root(outer, "inner")
    assert resolve_catalog_dir(None, roots=[outer, inner], cwd=inner) == inner / "translations"


def test_a_sibling_directory_of_an_app_package_is_refused(tmp_path):
    """``<app>/locale`` and ``<app>/../translations`` are both invisible.

    The loader reads exactly ``<root>/translations/<domain>.<lang>.json``, so
    "close to an app package" is not the test — being that path is.
    """
    app = _app_root(tmp_path)
    for bad in (app / "locale", tmp_path / "translations", app / "translations" / "ru"):
        with pytest.raises(CatalogDirError):
            resolve_catalog_dir(bad, roots=[app], cwd=app)


def test_an_absolute_out_into_another_app_package_is_accepted(tmp_path):
    """Naming a different installed app explicitly is legitimate — a host app
    may override another module's keys."""
    a = _app_root(tmp_path, "app_a")
    b = _app_root(tmp_path, "app_b")
    assert resolve_catalog_dir(b / "translations", roots=[a, b], cwd=a) == b / "translations"


def test_the_resolved_dir_is_one_the_loader_actually_reads(tmp_path):
    """The round trip that closes the loop: write where the resolver says, then
    read it back through the loader. This is the assertion the old default
    could not have passed."""
    app = _app_root(tmp_path)
    out = resolve_catalog_dir(None, roots=[app], cwd=app)
    translate_catalog("errors", "ru", out, source_texts=SOURCE,
                      seed={k: f"ru:{v}" for k, v in SOURCE.items()})
    assert load_app_catalogs("errors", "ru", dirs=[app]) == {
        k: f"ru:{v}" for k, v in SOURCE.items()}


def test_search_dirs_are_the_loader_s_own_roots():
    """The resolver and the loader must not keep separate ideas of "where".

    Both go through :func:`catalog_search_dirs`, so an installed app is
    writable exactly when it is readable.
    """
    roots = catalog_search_dirs()
    assert roots and all(isinstance(r, Path) for r in roots)
    resolved = resolve_catalog_dir(None, cwd=roots[0])
    assert resolved == Path(roots[0]).resolve() / "translations"


# ---------------------------------------------------------------------------
# The command surface — where both defects were actually reachable
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_domain():
    from stapel_core.i18n.domains import DOMAIN_SOURCES

    DOMAIN_SOURCES["_test_domain"] = lambda: dict(SOURCE)
    yield "_test_domain"
    DOMAIN_SOURCES.pop("_test_domain", None)


def _translate_command():
    # The core is not an installed app in this test project, so the command is
    # driven as an object (the tests/test_access_dac_report.py pattern).
    from stapel_core.django.management.commands.translate_catalogs import Command

    return Command()


def test_command_refuses_to_write_where_the_loader_cannot_see(
        tmp_path, monkeypatch, temp_domain):
    """Run from a service root, the command used to write ``./translations``
    and print a success line. Now it fails, and says where to write instead."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    monkeypatch.chdir(tmp_path)
    with pytest.raises(CommandError) as exc:
        call_command(_translate_command(), "--domain", temp_domain, "--lang", "ru")
    assert "--app" in str(exc.value)
    assert not (tmp_path / "translations").exists()  # nothing written anywhere


def test_command_writes_where_the_loader_reads_and_reports_it_unreviewed(
        tmp_path, monkeypatch, settings, temp_domain):
    """The success path, end to end, with the success made falsifiable.

    An ``EXTRA_CATALOG_DIRS`` root is a catalog root like any app package, so
    the default resolves there, and the very same ``load_app_catalogs`` the
    runtime uses reads back what the command wrote. The run also declares its
    output unreviewed — the seeded values are cheap, not blessed.
    """
    from stapel_core.i18n.conf import i18n_settings

    root = tmp_path / "config_repo"
    root.mkdir()
    settings.STAPEL_I18N = {"EXTRA_CATALOG_DIRS": [str(root)]}
    i18n_settings.reload()
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps({k: f"ru:{v}" for k, v in SOURCE.items()}),
                         encoding="utf-8")
    monkeypatch.chdir(root)

    from django.core.management import call_command

    out = io.StringIO()
    try:
        call_command(_translate_command(), "--domain", temp_domain, "--lang", "ru",
                     "--seed", str(seed_file), "--seed-label", "stapel-builtin",
                     stdout=out)
        assert (root / "translations" / f"{temp_domain}.ru.json").is_file()
        assert load_app_catalogs(temp_domain, "ru") == {
            k: f"ru:{v}" for k, v in SOURCE.items()}
        assert "unreviewed" in out.getvalue()
    finally:
        i18n_settings.reload()
