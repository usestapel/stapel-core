"""emit-check — static gate for outbox-atomicity discipline.

The outbox guarantee ("the event leaves iff the surrounding transaction
commits") was independently broken the same two ways by different modules
(categories C1, listings L2). This checker turns the discipline into a CI
gate. Rules:

- **EMIT001** — emit call inside an ``except`` handler. Emitting as an error
  fallback publishes an event about a mutation that is rolling back (or never
  happened).
- **EMIT002** — emit call inside a ``try`` whose handler catches broad
  ``Exception``/``BaseException``/bare and never re-raises: the C1 bug — a
  swallowed emit failure lets the mutation commit without its event.
- **EMIT003** — a function that both writes through the ORM (``save``/
  ``create``/``update``/``delete``/``bulk_*``) and emits, where the emit is
  not lexically inside ``transaction.atomic()`` / ``mutate_and_emit()`` and
  the function is not ``@transaction.atomic``-decorated: the L2 bug — save
  and outbox row in different transactions.
- **EMIT004** — emit inside a callback passed to ``transaction.on_commit``:
  the outbox row would be written *after* commit; a crash in between loses
  the event.

What counts as an emit call: any call to a name (or attribute) that is
``emit`` or starts with ``emit_`` — the stapel convention for outbox emit
helpers (``events.emit_listing_published`` etc.).

Suppression: append ``# emit-check: ok — <reason>`` to the flagged line
(e.g. when the caller provably holds the atomic block).

KNOWN LIMITATIONS (by design — this is a pragmatic AST pass, not data-flow
analysis):

- purely lexical: it cannot see that a *caller* wraps the function in
  ``transaction.atomic()`` (suppress with the pragma), nor that a callee
  opens one internally;
- name-based: an emit wrapper not named ``emit``/``emit_*`` (e.g.
  ``publish_category_changed``) is invisible to EMIT003/EMIT004 unless the
  call site also matches;
- EMIT003 only checks that the *emit* is inside an atomic construct, not
  that every ORM write shares it.

The runtime guards in ``stapel_core.comm.emit`` (EMIT_OUTSIDE_ATOMIC mode +
rollback-only marking on emit failure) cover what this static pass cannot.

Usage: ``python -m stapel_core.lint.emit_check [PATH ...]`` (default ``.``).
Skips tests, migrations, build artifacts and virtualenvs. Exit code 1 on
findings.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

PRAGMA = "emit-check: ok"

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    ".tox",
    "build",
    "dist",
    "node_modules",
    "__pycache__",
    "migrations",
    "tests",
}
EXCLUDED_FILES = {"conftest.py", "setup.py"}

ORM_WRITE_METHODS = {
    "save",
    "create",
    "update",
    "delete",
    "bulk_create",
    "bulk_update",
    "get_or_create",
    "update_or_create",
}

ATOMIC_CONTEXTS = {"atomic", "mutate_and_emit"}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _is_emit_call(node: ast.Call) -> bool:
    name = _call_name(node)
    return name is not None and (name == "emit" or name.startswith("emit_"))


def _is_orm_write(node: ast.Call) -> bool:
    # Only attribute calls (obj.save(), Model.objects.create(), ...) — a bare
    # create()/update() name is most likely not the ORM.
    return isinstance(node.func, ast.Attribute) and node.func.attr in ORM_WRITE_METHODS


def _is_atomic_with(node: ast.With) -> bool:
    for item in node.items:
        expr = item.context_expr
        target = expr.func if isinstance(expr, ast.Call) else expr
        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            name = target.attr
        if name in ATOMIC_CONTEXTS:
            return True
    return False


def _has_atomic_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            name = target.attr
        if name == "atomic":
            return True
    return False


def _handler_swallows(handler: ast.ExceptHandler) -> bool:
    """Broad handler (Exception/BaseException/bare) with no raise inside."""
    if handler.type is not None:
        names = []
        types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for t in types:
            if isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, ast.Attribute):
                names.append(t.attr)
        if not any(n in ("Exception", "BaseException") for n in names):
            return False
    return not any(
        isinstance(n, ast.Raise) for stmt in handler.body for n in ast.walk(stmt)
    )


class _Finding:
    __slots__ = ("path", "line", "code", "message")

    def __init__(self, path: Path, line: int, code: str, message: str) -> None:
        self.path = path
        self.line = line
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}"


def check_source(source: str, path: Path) -> list[_Finding]:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [_Finding(path, exc.lineno or 0, "EMIT000", f"syntax error: {exc.msg}")]

    lines = source.splitlines()

    def suppressed(node: ast.AST) -> bool:
        line = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
        return PRAGMA in line

    # Parent links for lexical-ancestry questions.
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def ancestors(node: ast.AST):
        cur = parents.get(node)
        while cur is not None:
            yield cur
            cur = parents.get(cur)

    findings: list[_Finding] = []
    emit_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_emit_call(n)]

    for call in emit_calls:
        if suppressed(call):
            continue
        chain = list(ancestors(call))

        # EMIT001 — emit lexically inside an except handler.
        if any(isinstance(a, ast.ExceptHandler) for a in chain):
            findings.append(_Finding(
                path, call.lineno, "EMIT001",
                "emit inside an except handler — publishes an event for a "
                "mutation that failed/rolled back",
            ))
            continue

        # EMIT002 — emit inside a try whose broad handler swallows. Walk up
        # to the innermost enclosing function only (a try in an outer
        # function does not swallow this call's failures lexically).
        swallowed = False
        prev: ast.AST = call
        for a in chain:
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                break
            if isinstance(a, ast.Try):
                in_guarded_body = prev in a.body  # else/finally aren't caught
                if in_guarded_body and any(_handler_swallows(h) for h in a.handlers):
                    swallowed = True
                    break
            prev = a
        if swallowed:
            findings.append(_Finding(
                path, call.lineno, "EMIT002",
                "emit failure swallowed by broad except — the mutation can "
                "commit without its event (categories C1); let it propagate "
                "or re-raise",
            ))
            continue

        # EMIT004 — emit inside an on_commit callback (lambda or nested def
        # passed to transaction.on_commit).
        flagged_on_commit = False
        for a in chain:
            if isinstance(a, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
                outer = parents.get(a)
                # lambda directly in the on_commit(...) argument list
                if isinstance(outer, ast.Call):
                    outer_name = _call_name(outer)
                    if outer_name == "on_commit":
                        findings.append(_Finding(
                            path, call.lineno, "EMIT004",
                            "emit inside an on_commit callback — the outbox row "
                            "is written after commit; a crash in between loses "
                            "the event. Emit inside the transaction instead",
                        ))
                        flagged_on_commit = True
                break
        if flagged_on_commit:
            continue

        # EMIT003 — enclosing function does ORM writes, emit not under an
        # atomic construct.
        func = next((a for a in chain if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        if func is None:
            continue
        if _has_atomic_decorator(func):
            continue
        withs = [a for a in chain if isinstance(a, ast.With)]
        if any(_is_atomic_with(w) for w in withs):
            continue
        does_orm_write = any(
            isinstance(n, ast.Call) and _is_orm_write(n) and n is not call
            for n in ast.walk(func)
        )
        if does_orm_write:
            findings.append(_Finding(
                path, call.lineno, "EMIT003",
                "mutation and emit in the same function without a shared "
                "transaction.atomic()/mutate_and_emit() — save and outbox row "
                "commit separately (listings L2). Wrap mutation+emit in "
                "stapel_core.comm.mutate_and_emit()",
            ))

    return findings


def iter_python_files(paths: list[Path]):
    for root in paths:
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        for p in sorted(root.rglob("*.py")):
            rel_parts = p.relative_to(root).parts
            if any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in rel_parts[:-1]):
                continue
            if p.name in EXCLUDED_FILES or p.name.startswith("test_"):
                continue
            yield p


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    roots = [Path(a) for a in args] or [Path(".")]
    findings: list[_Finding] = []
    for path in iter_python_files(roots):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(check_source(source, path))
    for f in findings:
        print(f)
    if findings:
        print(
            f"emit-check: {len(findings)} problem(s). "
            f"False positive (e.g. caller holds the atomic block)? "
            f"Append '# {PRAGMA} — <reason>' to the line.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
