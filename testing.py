"""
Test configuration helpers for standalone stapel-* packages.

Usage in conftest.py:
    from stapel_core.testing import configure_django
    configure_django(
        installed_apps=[
            'stapel_auth',
            'stapel_auth.migrations',
        ],
    )
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import django
from django.conf import settings


BASE_INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
]

BASE_MIDDLEWARE = [
    'django.middleware.common.CommonMiddleware',
]

BASE_REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'stapel_core.django.jwt.authentication.JWTCookieAuthentication',
    ],
    # Empty by default — avoid IsAuthenticated/IsServiceRequest blocking tests with 403
    'DEFAULT_PERMISSION_CLASSES': [],
    'EXCEPTION_HANDLER': 'stapel_core.django.api.errors.stapel_exception_handler',
}


def test_database() -> dict:
    """SQLite in memory, or the server ``STAPEL_TEST_DATABASE_URL`` names.

    Concurrency cannot be tested on ``:memory:`` — every connection gets its
    own database, so two writers never meet. A suite that needs the real
    interleaving (the first-contact INSERT race) points this at a
    Postgres and marks its test to skip without one; everything else keeps
    the fast in-process default. Env-gated rather than a settings knob so
    that one CI job, and no source change, decides.
    """
    url = os.environ.get("STAPEL_TEST_DATABASE_URL", "")
    if not url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}

    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": (parsed.path or "/postgres").lstrip("/"),
        "USER": unquote(parsed.username or "postgres"),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "127.0.0.1",
        "PORT": str(parsed.port or 5432),
    }


def configure_django(
    *,
    installed_apps: list[str],
    extra_settings: dict | None = None,
    middleware: list[str] | None = None,
    rest_framework: dict | None = None,
) -> None:
    """Configure Django for in-process package tests with SQLite.

    Call once from conftest.py before any imports that trigger Django setup.
    Safe to call multiple times — subsequent calls are no-ops if already configured.
    """
    if settings.configured:
        if not django.conf._wrapped:  # type: ignore[attr-defined]
            django.setup()
        return

    all_apps = BASE_INSTALLED_APPS + installed_apps

    settings.configure(
        SECRET_KEY='test-secret-key-not-for-production',
        DATABASES={'default': test_database()},
        INSTALLED_APPS=all_apps,
        MIDDLEWARE=middleware if middleware is not None else BASE_MIDDLEWARE,
        ROOT_URLCONF='',
        ALLOWED_HOSTS=['*'],
        USE_TZ=True,
        REST_FRAMEWORK=rest_framework if rest_framework is not None else BASE_REST_FRAMEWORK,
        STAPEL_AUTH={
            'JWT_SECRET': 'test-jwt-secret',
            'JWT_ALGORITHM': 'HS256',
            'ACCESS_TOKEN_LIFETIME_SECONDS': 900,
            'REFRESH_TOKEN_LIFETIME_SECONDS': 604800,
        },
        **(extra_settings or {}),
    )
    django.setup()


# ---------------------------------------------------------------------------
# Walking a repo's OWN sources
# ---------------------------------------------------------------------------
#
# Three separate incidents in one day, all the same shape: a test that wanted
# "our package's Python files" implemented it as "every *.py under the repo
# root, minus a hand-written list of directory names". That list is an
# open-world enumeration of a closed-world question, and it goes stale the
# moment someone names a virtualenv something nobody listed — `env/`,
# `.direnv/python-3.12/`, a colleague's `venv312/`. The rule then reports an
# INSTALLED SIBLING LIBRARY's file as this repo's violation. A gate that
# accuses the wrong file is worse than no gate: the reader spends an
# afternoon chasing a defect that is not in their code.
#
# So foreignness is decided by MARKER, never by name:
#
#   * a virtualenv is a directory containing ``pyvenv.cfg`` — that is the
#     file that DEFINES a venv, whatever it is called;
#   * a tree with a ``site-packages`` component is installed or vendored code
#     even without a cfg;
#   * ``build/``/``dist/`` are excluded ONLY when they carry packaging
#     markers. A source directory legitimately named ``build`` must not be
#     silently skipped — that is the same stale-list disease, inverted.
#
# Walking the package's ``__path__`` instead is not sufficient on its own:
# stapel repos are flat (the repo root IS the package directory), so an
# in-repo ``.venv`` and a ``build/lib/<pkg>/`` tree sit INSIDE the package
# path. Marker-based foreignness is required either way.

#: Directories that are never a repo's own sources, under any layout.
_ALWAYS_FOREIGN = frozenset({"__pycache__", ".git", "node_modules", ".tox", ".mypy_cache"})

#: The file that defines a virtualenv, whatever the directory is called.
_VENV_MARKER = "pyvenv.cfg"


def _packaging_output_roots(root: Path) -> set[Path]:
    """``build``/``dist`` directories that really are packaging output.

    Checked by marker, not by name: ``build/lib/<pkg>/`` is the setuptools
    layout, and an ``*.egg-info``/``*.dist-info`` inside is the installed
    metadata. A ``build/`` directory holding hand-written sources — some
    repos really do have one — carries neither and stays included.
    """
    roots: set[Path] = set()
    for name in ("build", "dist"):
        candidate = root / name
        if not candidate.is_dir():
            continue
        looks_packaged = (candidate / "lib").is_dir() or any(
            child.name.endswith((".egg-info", ".dist-info"))
            for child in candidate.iterdir()
        )
        if looks_packaged:
            roots.add(candidate)
    return roots


def find_venv_roots(root: Path) -> set[Path]:
    """Virtualenv directories under *root*, found by ``pyvenv.cfg``."""
    return {cfg.parent for cfg in Path(root).rglob(_VENV_MARKER)}


def is_foreign_source(
    path: Path,
    root: Path,
    venv_roots: set[Path] | None = None,
    packaging_roots: set[Path] | None = None,
) -> bool:
    """Is *path* something other than *root*'s own source?

    Exposed separately from :func:`iter_own_sources` on purpose, and this is
    the lesson the prototype paid for: in CI there is no in-repo virtualenv,
    so a test that only walked the real tree would pass vacuously and the
    exclusion would rot exactly where it matters. Being a pure predicate, it
    is assertable on synthetic paths that no checkout has to contain.

    *venv_roots* and *packaging_roots* may be passed to avoid rescanning in a
    loop; when omitted they are discovered from *root*.
    """
    path, root = Path(path), Path(root)
    if venv_roots is None:
        venv_roots = find_venv_roots(root)
    if packaging_roots is None:
        packaging_roots = _packaging_output_roots(root)

    parts = set(path.parts)
    if parts & _ALWAYS_FOREIGN:
        return True
    if "site-packages" in parts:
        return True  # installed or vendored, with or without a pyvenv.cfg
    parents = set(path.parents)
    if any(venv in parents or venv == path for venv in venv_roots):
        return True
    if any(pkg in parents or pkg == path for pkg in packaging_roots):
        return True
    return False


def iter_own_sources(root: Path, suffix: str = ".py") -> Iterator[Path]:
    """Yield *root*'s own source files, sorted, foreign trees excluded.

    The one walk every gate in the fleet should use::

        from stapel_core.testing import iter_own_sources

        for path in iter_own_sources(REPO_ROOT):
            ...

    Deleting a local skip-list in favour of this call is the point: the list
    is what goes stale, and every copy of it goes stale independently.
    """
    root = Path(root)
    venv_roots = find_venv_roots(root)
    packaging_roots = _packaging_output_roots(root)
    for path in sorted(root.rglob(f"*{suffix}")):
        if is_foreign_source(path, root, venv_roots, packaging_roots):
            continue
        yield path


# ---------------------------------------------------------------------------
# The contract mount: a contract nothing drives is a contract nothing checks
# ---------------------------------------------------------------------------

#: Concrete values tried for each ``{param}`` of a declared path. Resolution
#: cares about the SHAPE of a segment, and the fleet's urlconfs use several
#: converters, so a path counts as reachable if ANY shape resolves: the
#: question is whether the mount exists, not whether a particular id does.
#:
#: A single uuid was the first cut and it reported false positives on
#: integer-keyed paths.
PATH_PARAM_CANDIDATES = (
    "00000000-0000-4000-8000-000000000000",
    "1",
    "a-slug",
)


def declared_paths(schema: dict) -> list:
    """Every path key in a committed OpenAPI document, sorted."""
    return sorted(schema.get("paths", {}))


def unresolved_declared_paths(schema: dict, urlconf=None) -> list:
    """Declared paths that do not resolve under *urlconf* (default: active)."""
    from django.urls import Resolver404, resolve

    import re as _re

    unresolved = []
    for path in declared_paths(schema):
        for value in PATH_PARAM_CANDIDATES:
            try:
                resolve(_re.sub(r"\{[^}]+\}", value, path), urlconf=urlconf)
                break
            except Resolver404:
                continue
        else:
            unresolved.append(path)
    return unresolved


def assert_declared_paths_resolve(schema: dict, urlconf=None) -> None:
    """Every path the committed document declares must resolve HERE.

    THE DEFECT THIS NAMES. Five of the first eight libraries swept had a test
    urlconf pointing somewhere their own ``docs/schema.json`` does not
    describe: one mounted a different prefix AND one segment short, one
    mounted the paths bare, one mounted less than the emission did, one
    skipped both the host's segment and the module's own, and one mounted a
    DOUBLED prefix. In every case the committed contract was never driven by
    anything, and the suite was green throughout.

    It is the same family as a gate nobody asks. Every recipe can be written,
    the run can be green, and not one request went where the contract says it
    goes — because when the mount is wrong, every operation is equally and
    silently unreachable, which no per-operation check can see.

    A library whose DEPLOYED prefix differs from its contract prefix keeps
    both: pass the contract urlconf here, and pin the deployed one in its own
    test. `stapel-workspaces` is the proof case — its suite mounts
    ``workspaces/api/workspaces/`` because that is one host's real prefix, and
    a separate test pins it deliberately. Two mounts answering two different
    questions is fine. Neither being asked about the contract is not.
    """
    unresolved = unresolved_declared_paths(schema, urlconf=urlconf)
    assert not unresolved, (
        "a contract nothing drives is a contract nothing checks: these paths "
        "are declared in the committed schema and do not resolve under this "
        "suite's urlconf, so nothing here can be exercising them. The MOUNT "
        "is wrong, not the recipes — fix the urlconf this test declares, or "
        "the emission it is meant to mirror:\n  " + "\n  ".join(unresolved)
    )
