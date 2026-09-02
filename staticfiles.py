"""Discovery of the ``static/`` directories shipped by EMBEDDED stapel libraries.

Django collects a library's static files by walking ``INSTALLED_APPS``
(``AppDirectoriesFinder``). Half the fleet's libraries are not apps: they are
*embedded* — imported directly, carrying no models and no ``AppConfig``, and
therefore never listed. ``stapel_attributes`` is the archetype. It ships the
per-kind config editor (the widget that edits a select's options, an int's
min/max) as ``static/stapel_attributes/attributes-admin.js``, and both
``stapel_categories``' feature editor and ``stapel_forms``' admin builder mount
it.

Nothing walks it. ``collectstatic`` never sees the file, the ``<script>`` tag
is emitted pointing at a URL that 404s, and the admin form renders, saves and
silently offers no way to edit a field's settings. There is no traceback, no
log line and no visual gap — the editor simply is not there.

The old answer was for every host to name every embedded library's directory
in its own ``STATICFILES_DIRS``. That is a rule enforced by memory, once per
service per library, and it had already been forgotten everywhere except the
one host where the symptom was noticed.

WHY A WALK AND NOT A REGISTRY
-----------------------------
An opt-in registry — an entry point, a module-level marker, a list this file
maintains — fails in exactly the way the original bug failed: a library that
ships assets and forgets to enrol is missed, silently, and the person who
finds out is a user staring at a widget that is not there. The registry would
have to be remembered at the same moment the ``STATICFILES_DIRS`` line had to
be remembered, so it moves the forgetting rather than removing it.

So discovery is by construction: any importable ``stapel_*`` package that has
a ``static/`` directory is collected, and a new library gets this for free on
the day it is installed.

WHAT IT COSTS
-------------
This runs inside a settings module, so it may not import anything. It does
not. One ``os.scandir()`` per ``sys.path`` directory yields both shapes at
once — packages sitting on the path (wheels, vendored checkouts) and
``.dist-info`` directories (which is how an editable install advertises code
that lives elsewhere) — and the handful of matches are resolved through
``importlib.util.find_spec``, which locates a top-level package without
executing it. Results are cached per ``sys.path``.

The output is sorted by package name: the same environment produces the same
list, whatever order ``sys.path`` happens to be in.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

#: Import-name prefix of the fleet's libraries. A ``.dist-info`` directory is
#: named after the NORMALISED distribution (``stapel-attributes`` becomes
#: ``stapel_attributes-0.9.0.dist-info``), so the one prefix matches both.
PACKAGE_PREFIX = "stapel_"

_METADATA_SUFFIXES = (".dist-info", ".egg-info")


def embedded_static_dirs() -> List[str]:
    """Every ``static/`` directory shipped by an installed ``stapel_*`` package.

    Sorted by package name, de-duplicated by real path. Safe to call from a
    settings module: it imports nothing.
    """
    return [static_dir for _name, static_dir in embedded_static_packages()]


def embedded_static_packages() -> Tuple[Tuple[str, str], ...]:
    """``(package_name, static_dir)`` pairs — the same walk, named.

    The system check needs the names: a finding that says "some directory is
    unreachable" sends the reader looking, and one that says
    ``stapel_attributes`` does not.
    """
    return _scan(tuple(sys.path))


def reset_cache() -> None:
    """Drop the memoised walk (tests, and any process that mutates sys.path)."""
    _scan.cache_clear()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _scan(path_entries: Tuple[str, ...]) -> Tuple[Tuple[str, str], ...]:
    found: List[Tuple[str, str]] = []
    seen_real: set = set()
    for name in _candidate_packages(path_entries):
        static_dir = _static_dir_of(name)
        if static_dir is None:
            continue
        real = os.path.realpath(static_dir)
        if real in seen_real:
            continue
        seen_real.add(real)
        found.append((name, static_dir))
    return tuple(found)


def _candidate_packages(path_entries: Sequence[str]) -> List[str]:
    """Import names of ``stapel_*`` packages, from one scandir per path entry.

    Two shapes are collected in the same pass because a deployment can be
    either, and the fleet is both:

    * a directory ``stapel_x/`` holding ``__init__.py`` — a wheel installed
      into site-packages, or a checkout vendored into the image;
    * a ``stapel_x-<version>.dist-info/`` — an editable install, whose code is
      behind an import hook and is nowhere on the path as a directory.
    """
    names: Dict[str, None] = {}  # ordered set; sorted on the way out
    scanned: set = set()
    for entry in path_entries:
        directory = entry or os.getcwd()
        try:
            real = os.path.realpath(directory)
        except OSError:  # pragma: no cover - defensive
            continue
        if real in scanned:
            continue
        scanned.add(real)
        try:
            children = list(os.scandir(directory))
        except (OSError, ValueError):
            # A zip import, a missing path entry, a permission wall — none of
            # them are this function's problem to report.
            continue
        for child in children:
            child_name = child.name
            if not child_name.startswith(PACKAGE_PREFIX):
                continue
            try:
                if not child.is_dir():
                    continue
            except OSError:  # pragma: no cover - defensive
                continue
            if child_name.endswith(_METADATA_SUFFIXES):
                for name in _names_from_metadata(child.path, child_name):
                    names.setdefault(name, None)
            elif os.path.isfile(os.path.join(child.path, "__init__.py")):
                names.setdefault(child_name, None)
    return sorted(names)


def _names_from_metadata(info_path: str, info_name: str) -> List[str]:
    """Top-level import names advertised by a ``.dist-info``/``.egg-info``.

    ``top_level.txt`` when setuptools wrote one; otherwise the distribution
    name itself, which is the fleet's convention (``stapel-attributes`` ships
    ``stapel_attributes``) and is what modern backends leave us with.
    """
    top_level = os.path.join(info_path, "top_level.txt")
    try:
        with open(top_level, "r", encoding="utf-8") as handle:
            declared = [line.strip() for line in handle if line.strip()]
    except OSError:
        declared = []
    if declared:
        return [name for name in declared if name.startswith(PACKAGE_PREFIX)]
    stem = info_name
    for suffix in _METADATA_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    # "stapel_attributes-0.9.0" -> "stapel_attributes"
    return [stem.split("-", 1)[0]]


def _static_dir_of(name: str) -> Optional[str]:
    """The package's ``static/`` directory, resolved WITHOUT importing it.

    ``find_spec`` on a top-level name runs the path finders only; it does not
    execute the module. That matters twice over: settings modules cannot
    afford the import time, and a library that imports Django at module level
    (every widget module does) would be a circular import at this point.
    """
    from importlib.util import find_spec

    try:
        spec = find_spec(name)
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None:
        return None
    for location in spec.submodule_search_locations or ():
        static_dir = os.path.join(location, "static")
        if os.path.isdir(static_dir):
            return static_dir
    return None


__all__ = [
    "PACKAGE_PREFIX",
    "embedded_static_dirs",
    "embedded_static_packages",
    "reset_cache",
]
