"""Layer boundary of the kernel wheel (rule R1 ``core-layers``).

``stapel_core`` is one distribution with three declared layers:

* **L0 base** — settings plumbing and security primitives. Imports stdlib,
  Django and other L0 units only.
* **L1 kernel** — what every service runs: comm, bus, the Django layer, JWT,
  access, gdpr, observability, …
* **L2 optional features** — parts a service may never touch: captcha,
  netintel classification, gateway, media, sitemap, the client halves of
  cdn/workspaces, oauth, the kafka shim, dev tooling.

The rule: **a lower layer never imports a higher one at module level, and
imports it lazily (inside a function) only through an allow-listed pair.**
That is what keeps "core grew a feature every service now carries" a red
build instead of a feeling. L2 units may import each other (captcha reads
netintel); the rule constrains direction between layers only.

What this gate cannot see: imports spelled as strings (``import_module``,
``import_string``, dotted paths in settings and the root ``_LAZY_EXPORTS``)
— those resolve at runtime and are not edges of the static graph. An
``if TYPE_CHECKING:`` block is not an import at runtime and is skipped.

Every unit must be assigned a layer: a new top-level module or ``django/``
submodule fails here until someone decides where it belongs.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from stapel_core.testing import iter_own_sources

REPO = Path(__file__).resolve().parent.parent

L0, L1, L2 = 0, 1, 2

#: Unit -> layer. A unit is a top-level module/package of ``stapel_core``,
#: or ``django/<name>`` for the Django layer's direct children.
LAYERS: dict[str, int] = {
    # L0 base
    "conf": L0,
    "conf_checks": L0,
    "config": L0,
    "secrets": L0,
    "security": L0,
    "signals": L0,
    "hashing": L0,
    "schema_strict": L0,
    "net": L0,
    # L1 kernel
    "<root>": L1,
    "access": L1,
    "bus": L1,
    "comm": L1,
    "core": L1,
    "eventstore": L1,
    "flows": L1,
    "gdpr": L1,
    "i18n": L1,
    "language": L1,
    "notifications": L1,
    "observability": L1,
    "sites": L1,
    "staticfiles": L1,
    "templates": L1,
    "verification": L1,
    "django": L1,
    # A constants shim (event types, topic names): ``notifications.publish``
    # imports it at module level, so it is kernel by use.
    "kafka": L1,
    # L2 optional features
    "captcha": L2,
    "netintel": L2,
    "gateway": L2,
    "media": L2,
    "oauth": L2,
    "lint": L2,
    "testing": L2,
}

#: ``django/<name>`` units that are L2; every other ``django/*`` unit is L1.
DJANGO_L2 = frozenset({
    "django/captcha",
    "django/captcha_checks",
    "django/gateway",
    "django/sitemap",
    "django/workspaces",
})
# ``django/cdn`` is the client half of the cdn seam and is L1:
# ``stapel_core.django`` re-exports ``CdnImageField`` at module level.

#: The lazy (function-level) lower→higher imports that are allowed, as
#: ``(importer module, imported module)``. Adding a pair here is a design
#: decision, not a way to make this file green: say why next to it.
LAZY_ALLOWED: frozenset[tuple[str, str]] = frozenset({
    # System-check registration in CommonDjangoConfig.ready(): netintel's
    # provider checks (W001-W003).
    ("stapel_core.django.apps", "stapel_core.netintel.checks"),
    # System-check registration in CommonDjangoConfig.ready(): captcha
    # backend checks.
    ("stapel_core.django.apps", "stapel_core.django.captcha_checks"),
    # The OpenAPI contract profile reads which views are captcha-guarded
    # (x-stapel-captcha) while the schema is generated, never at boot.
    ("stapel_core.django.openapi.contract", "stapel_core.django.captcha"),
    # `manage.py stapel_preflight` probes the workspaces peer client when
    # the command runs.
    ("stapel_core.django.management.commands.stapel_preflight",
     "stapel_core.django.workspaces"),
})


def unit_of(module: str) -> str:
    """``stapel_core.django.jwt.utils`` -> ``django/jwt``; ``stapel_core.comm.x`` -> ``comm``."""
    parts = module.split(".")[1:]
    if not parts:
        return "<root>"
    if parts[0] == "django" and len(parts) > 1:
        return f"django/{parts[1]}"
    return parts[0]


def layer_of(unit: str) -> int | None:
    if unit.startswith("django/"):
        return L2 if unit in DJANGO_L2 else LAYERS.get("django")
    return LAYERS.get(unit)


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["stapel_core", *parts])


def _is_package(root: Path, module: str) -> bool:
    rel = Path(*module.split(".")[1:]) if "." in module else Path()
    return (root / rel / "__init__.py").is_file()


def _exists(root: Path, module: str) -> bool:
    rel = Path(*module.split(".")[1:]) if "." in module else Path()
    return (root / rel / "__init__.py").is_file() or (root / rel).with_suffix(".py").is_file()


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(tree: ast.AST):
    """Yield ``(node, eager)`` for every import statement in *tree*."""

    def walk(node, in_function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for sub in child.orelse:
                    yield from walk(ast.Module(body=[sub], type_ignores=[]), in_function)
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child, not in_function
            nested = in_function or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            )
            yield from walk(child, nested)

    yield from walk(tree, False)


def import_edges(root: Path):
    """``(importer, imported, eager)`` for every in-package import under *root*."""
    edges = []
    for path in iter_own_sources(root):
        rel = path.relative_to(root)
        if rel.parts[0] in {"tests", "docs"}:
            continue
        importer = _module_name(root, path)
        package = importer if path.name == "__init__.py" else importer.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, eager in _imports(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                if node.level:
                    base = package.split(".")
                    base = base[: len(base) - (node.level - 1)]
                    mod = ".".join(base + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                # ``from pkg import submodule`` names a module, not an
                # attribute: the edge goes to the submodule. Any other name
                # is an attribute of ``mod`` itself.
                submodules = [
                    f"{mod}.{alias.name}" for alias in node.names
                    if mod.startswith("stapel_core") and _is_package(root, mod)
                    and _exists(root, f"{mod}.{alias.name}")
                ]
                attributes = len(submodules) < len(node.names)
                targets = submodules + ([mod] if attributes else [])
            for target in targets:
                if target == "stapel_core" or target.startswith("stapel_core."):
                    edges.append((importer, target, eager))
    return edges


def violations(root: Path):
    """``(kind, importer, imported)`` findings for the tree at *root*."""
    found = []
    units = set()
    for path in iter_own_sources(root):
        rel = path.relative_to(root)
        if rel.parts[0] in {"tests", "docs"}:
            continue
        units.add(unit_of(_module_name(root, path)))
    for unit in sorted(units):
        if layer_of(unit) is None:
            found.append(("unassigned", unit, ""))
    for importer, target, eager in import_edges(root):
        src, dst = layer_of(unit_of(importer)), layer_of(unit_of(target))
        if src is None or dst is None or dst <= src:
            continue
        if eager:
            found.append(("eager", importer, target))
        elif (importer, target) not in LAZY_ALLOWED:
            found.append(("lazy", importer, target))
    return sorted(set(found))


def _lazy_upward_pairs(root: Path):
    pairs = set()
    for importer, target, eager in import_edges(root):
        src, dst = layer_of(unit_of(importer)), layer_of(unit_of(target))
        if not eager and src is not None and dst is not None and dst > src:
            pairs.add((importer, target))
    return pairs


def test_no_layer_violations():
    assert violations(REPO) == []


def test_allow_list_has_no_dead_entries():
    """An allow-listed pair that no longer occurs is removed, not kept "just in case"."""
    assert LAZY_ALLOWED <= _lazy_upward_pairs(REPO)


def test_the_scan_saw_the_package():
    """A green that walked nothing proves nothing."""
    edges = import_edges(REPO)
    assert len(edges) > 500
    assert ("stapel_core.django.apps", "stapel_core.netintel.checks", False) in edges


# --------------------------------------------------------------------------- #
# Inversion: the gate goes red on each defect it exists for.
# --------------------------------------------------------------------------- #

def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "stapel_core"
    for path in iter_own_sources(REPO):
        rel = path.relative_to(REPO)
        if rel.parts[0] in {"tests", "docs"}:
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return root


def test_inversion_eager_kernel_to_optional_is_red(tmp_path):
    root = _copy(tmp_path)
    target = root / "comm" / "__init__.py"
    target.write_text(target.read_text() + "\nimport stapel_core.captcha  # noqa\n")
    assert ("eager", "stapel_core.comm", "stapel_core.captcha") in violations(root)


def test_inversion_eager_base_to_kernel_is_red(tmp_path):
    root = _copy(tmp_path)
    target = root / "security" / "conf.py"
    target.write_text(target.read_text() + "\nfrom stapel_core import comm  # noqa\n")
    assert ("eager", "stapel_core.security.conf", "stapel_core.comm") in violations(root)


def test_inversion_relative_import_is_resolved(tmp_path):
    root = _copy(tmp_path)
    target = root / "django" / "mounts.py"
    target.write_text(target.read_text() + "\nfrom . import captcha  # noqa\n")
    assert ("eager", "stapel_core.django.mounts", "stapel_core.django.captcha") in violations(root)


def test_inversion_unlisted_lazy_import_is_red(tmp_path):
    root = _copy(tmp_path)
    target = root / "bus" / "__init__.py"
    target.write_text(
        target.read_text()
        + "\n\ndef _probe():\n    from stapel_core.gateway import ratelimit  # noqa\n"
    )
    assert ("lazy", "stapel_core.bus", "stapel_core.gateway.ratelimit") in violations(root)


def test_inversion_unassigned_unit_is_red(tmp_path):
    root = _copy(tmp_path)
    (root / "brand_new_feature.py").write_text("X = 1\n")
    assert ("unassigned", "brand_new_feature", "") in violations(root)


def test_type_checking_and_downward_imports_stay_silent(tmp_path):
    root = _copy(tmp_path)
    target = root / "comm" / "__init__.py"
    target.write_text(
        target.read_text()
        + "\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    import stapel_core.gateway  # noqa\n"
    )
    target = root / "captcha" / "__init__.py"
    target.write_text(target.read_text() + "\nimport stapel_core.comm  # noqa\n")
    assert violations(root) == []


@pytest.mark.parametrize("unit", sorted(DJANGO_L2))
def test_declared_l2_django_units_exist(unit):
    """A layer table naming a unit that is gone has stopped describing the code."""
    path = REPO / unit
    assert path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()
