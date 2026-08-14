"""Two threads may enter any package's import graph at once.

Context (ironmemo stand, 2026-07-26): a service answered 502 after every
deploy until someone restarted it, and the cause was diagnosed as an
import-lock inversion specific to Python 3.14 — a package body doing
``from .base import X`` holds lock(pkg) while taking lock(pkg.base), while a
thread entering through ``pkg.base`` takes the same two in the opposite
order, because the parent is imported INSIDE the child's module lock. Both
orders occur under `runserver`: Django's system checks run on one thread
while the autoreloader pulls the URLconf on another.

**Honest scope, because this test's green is weaker than it looks.** Adding
3.14 to the CI matrix does not cover the class — a pytest suite is
single-threaded, so every module is imported once, from one thread, in a
settled order. Hence the deliberate race here (in a subprocess: re-importing
inside a live pytest process would corrupt its own state).

But this file has NOT been shown to fail on a known instance of the bug. On
3.14.6 the race passes against `stapel-agent` at the commit BEFORE its
deadlock fix, and against a synthetic package built to invert, so either the
reproduction needs conditions this harness does not create, or the diagnosis
was incomplete. Treat it as a net, not a proof: it fails loudly if a package
ever hangs or raises `_DeadlockError` under a two-thread import, and it says
nothing stronger than that.

`stapel_core` deliberately keeps `from .submodule import X` in package bodies
— `from stapel_core.comm import call` IS the public API — so the static
invariant used in stapel-agent (no package body imports its own submodules)
does not apply here. Restructuring 17 packages on an unreproduced hazard
would be a bigger change than the evidence supports.
"""
import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from stapel_core.testing import iter_own_sources

REPO = Path(__file__).resolve().parent.parent

#: Packages whose bodies re-export from their own submodules — the shape that
#: can invert. Derived from the source, not listed by hand, so a NEW package
#: with the same shape is covered the day it appears.
def _packages_importing_own_submodules() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    # The local skip-list this used to carry is deleted, not extended: a
    # venv named anything but ".venv" made this rule read an installed
    # sibling library and report it as this repo's violation.
    for init in iter_own_sources(REPO, suffix="__init__.py"):
        if "tests" in init.parts:
            continue  # test packages are not the library's import graph
        rel = init.relative_to(REPO).parent
        if not rel.parts:
            continue  # the top-level package itself has no parent to race
        package = "stapel_core." + ".".join(rel.parts)
        try:
            tree = ast.parse(init.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - would fail elsewhere first
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                # `from .submodule import ...` inside the package body
                pairs.append((package, f"{package}.{node.module}"))
    return sorted(set(pairs))


RACE = textwrap.dedent(
    """
    import importlib, sys, threading
    parent, child = sys.argv[1], sys.argv[2]
    barrier = threading.Barrier(2)
    failures = []

    def load(name):
        try:
            barrier.wait()
            importlib.import_module(name)
        except BaseException as exc:            # noqa: BLE001 - reported below
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=load, args=(n,)) for n in (parent, child)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)
    if any(t.is_alive() for t in threads):
        print("DEADLOCK")
        sys.exit(2)
    for f in failures:
        if "_DeadlockError" in f or "deadlock" in f.lower():
            print(f)
            sys.exit(2)
    sys.exit(0)
    """
)


@pytest.mark.parametrize(
    "package,submodule",
    _packages_importing_own_submodules(),
    ids=lambda v: v.replace("stapel_core.", ""),
)
def test_two_threads_may_enter_the_import_graph_from_either_side(package, submodule):
    result = subprocess.run(
        [sys.executable, "-c", RACE, package, submodule],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(REPO.parent),
    )
    assert result.returncode != 2, (
        f"{package} deadlocks when raced against {submodule}: "
        f"{result.stdout.strip() or 'thread never finished'}\n"
        "Move the submodule import inside the function that needs it, so no "
        "package body holds its own lock while acquiring a submodule's."
    )


def test_the_race_actually_covers_something():
    """A guard on the guard: if the discovery ever returns nothing, the
    parametrized test above silently passes by having no cases at all."""
    assert len(_packages_importing_own_submodules()) >= 10
