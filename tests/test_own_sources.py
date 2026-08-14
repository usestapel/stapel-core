""""Our package's sources" is a closed-world question; name lists answer it open-world.

Three incidents in one day, all identical: a gate walked ``rglob("*.py")``
from the repo root and skipped a hand-written list of directory names. The
day someone's virtualenv was called something not on the list, the gate read
an INSTALLED SIBLING LIBRARY's file and reported it as this repo's violation
— a red on a file the repo does not own, sending the reader to hunt a defect
that is not there.

Almost everything here runs on SYNTHETIC trees, and that is the load-bearing
design decision, not a convenience: in CI there is no in-repo virtualenv, so
a test that only walked the real checkout would pass vacuously and the
exclusion would rot in exactly the condition it exists for.
"""
from pathlib import Path

from stapel_core.testing import (
    find_venv_roots,
    is_foreign_source,
    iter_own_sources,
)


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# The predicate, on paths no checkout has to contain.
# ---------------------------------------------------------------------------

def test_a_venv_is_found_by_its_marker_not_its_name(tmp_path):
    """``.venv`` is a convention; ``pyvenv.cfg`` is the definition."""
    for name in (".venv", "venv", "env-anything", "direnv-py312"):
        write(tmp_path / name / "pyvenv.cfg", "home = /usr\n")
    assert find_venv_roots(tmp_path) == {
        tmp_path / name for name in (".venv", "venv", "env-anything", "direnv-py312")
    }


def test_files_inside_any_venv_are_foreign(tmp_path):
    venv = tmp_path / "env-nobody-would-have-listed"
    write(venv / "pyvenv.cfg", "home = /usr\n")
    installed = write(venv / "lib" / "python3.12" / "site-packages" / "sibling.py")
    assert is_foreign_source(installed, tmp_path) is True


def test_site_packages_without_a_cfg_is_still_foreign():
    """Vendored trees carry no pyvenv.cfg; the component name is the marker."""
    root = Path("/synthetic/repo")
    assert is_foreign_source(root / "vendor" / "site-packages" / "x.py", root) is True


def test_our_own_sources_are_not_foreign():
    root = Path("/synthetic/repo")
    assert is_foreign_source(root / "services.py", root) is False
    assert is_foreign_source(root / "django" / "jwt" / "provider.py", root) is False


def test_pycache_git_and_node_modules_are_foreign():
    root = Path("/synthetic/repo")
    for part in ("__pycache__", ".git", "node_modules"):
        assert is_foreign_source(root / part / "x.py", root) is True


def test_the_predicate_works_on_paths_that_do_not_exist():
    """Synthetic assertability is the whole point of splitting it out."""
    root = Path("/nowhere/at/all")
    assert is_foreign_source(root / "a.py", root, venv_roots=set(), packaging_roots=set()) is False


# ---------------------------------------------------------------------------
# build/ and dist/ — excluded by MARKER, never by name.
# ---------------------------------------------------------------------------

def test_setuptools_build_lib_layout_is_excluded(tmp_path):
    write(tmp_path / "build" / "lib" / "pkg" / "copy.py")
    write(tmp_path / "own.py")
    assert [p.name for p in iter_own_sources(tmp_path)] == ["own.py"]


def test_build_with_egg_info_is_excluded(tmp_path):
    write(tmp_path / "build" / "pkg.egg-info" / "PKG-INFO", "Name: pkg\n")
    write(tmp_path / "build" / "stale.py")
    write(tmp_path / "own.py")
    assert [p.name for p in iter_own_sources(tmp_path)] == ["own.py"]


def test_a_plain_build_directory_is_INCLUDED(tmp_path):
    """A source dir that happens to be named ``build`` is still ours.

    Skipping it on the name alone would be the same stale-list disease
    inverted: the gate silently stops reading real code, and silence is the
    failure mode nobody notices.
    """
    write(tmp_path / "build" / "generator.py")
    write(tmp_path / "own.py")
    assert sorted(p.name for p in iter_own_sources(tmp_path)) == [
        "generator.py", "own.py",
    ]


def test_dist_with_packaging_markers_is_excluded(tmp_path):
    write(tmp_path / "dist" / "pkg.dist-info" / "METADATA", "Name: pkg\n")
    write(tmp_path / "dist" / "wheel_extract.py")
    write(tmp_path / "own.py")
    assert [p.name for p in iter_own_sources(tmp_path)] == ["own.py"]


# ---------------------------------------------------------------------------
# The walk.
# ---------------------------------------------------------------------------

def test_the_walk_is_the_repo_sources_and_nothing_else(tmp_path):
    """The exact scenario every hand-rolled walk got wrong."""
    write(tmp_path / "conf.py")
    write(tmp_path / "pkg" / "models.py")
    write(tmp_path / "tests" / "test_x.py")

    venv = tmp_path / "env312"  # a name no skip-list contained
    write(venv / "pyvenv.cfg", "home = /usr\n")
    write(venv / "lib" / "python3.12" / "site-packages" / "stapel_core" / "conf.py")
    write(tmp_path / "build" / "lib" / "pkg" / "models.py")
    write(tmp_path / "node_modules" / "thing" / "setup.py")
    write(tmp_path / "pkg" / "__pycache__" / "models.cpython-312.pyc")

    found = {p.relative_to(tmp_path).as_posix() for p in iter_own_sources(tmp_path)}
    assert found == {"conf.py", "pkg/models.py", "tests/test_x.py"}


def test_the_walk_is_sorted_and_deterministic(tmp_path):
    for name in ("c.py", "a.py", "b.py"):
        write(tmp_path / name)
    assert [p.name for p in iter_own_sources(tmp_path)] == ["a.py", "b.py", "c.py"]


def test_the_suffix_is_configurable(tmp_path):
    write(tmp_path / "a.py")
    write(tmp_path / "schema.json", "{}")
    venv = tmp_path / ".venv"
    write(venv / "pyvenv.cfg", "home = /usr\n")
    write(venv / "vendored.json", "{}")
    assert [p.name for p in iter_own_sources(tmp_path, suffix=".json")] == ["schema.json"]


def test_an_empty_tree_yields_nothing(tmp_path):
    assert list(iter_own_sources(tmp_path)) == []


def test_it_walks_this_very_repo_without_reading_the_installed_copy():
    """The real tree, as a smoke test for the shape stapel repos actually have.

    stapel repos are flat — the repo root IS the package directory — so an
    in-repo ``.venv`` and ``build/lib/<pkg>/`` sit INSIDE the package path.
    That is why "walk the package's ``__path__``" is not sufficient on its
    own and marker-based foreignness is required regardless.
    """
    repo_root = Path(__file__).resolve().parent.parent
    paths = list(iter_own_sources(repo_root))
    assert any(p.name == "conf.py" for p in paths)
    assert not any("site-packages" in p.parts for p in paths)
    assert not any("__pycache__" in p.parts for p in paths)
