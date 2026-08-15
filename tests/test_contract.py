"""Drift gate for ``docs/capabilities.json`` — the core's contract document.

stapel-core was the fleet's only significant library with no
``docs/capabilities.json``. The reason was structural, not neglectful: the
format described the CONFIGURATION surface (``axes``) and the SUBSTITUTION
surface (``extension_points``), and the core has no feature axes and no OpenAPI
operations — it had nothing to say in the format. What it does have is the
USAGE surface: the permission classes, factories, predicates and templates a
product is supposed to call. That is the ``surface`` section
(discoverability-design.md §1.2–§1.3, :mod:`stapel_tools.surface`).

Two things are gated here, and the second one is the point:

1. the committed document matches a fresh emission (``make contract``);
2. every symbol the declared roots select carries a curated ``intent`` — the
   emitter fails LOUDLY otherwise, so this test cannot pass while an export
   sits there unexplained. A library that exports a mechanism it cannot
   describe has just built the next mechanism nobody adopts; that is exactly
   how six of them shipped unused in one night.
"""
import json
from pathlib import Path

import pytest

try:
    import stapel_tools  # noqa: F401  (probe: the emitter must be importable)
except ImportError as exc:  # pragma: no cover - environment failure, not a branch
    # NOT pytest.importorskip. A drift gate that skips when its emitter is
    # missing reports `1 skipped`, exits 0, and disappears among a hundred
    # green tests — making "the tool is absent" indistinguishable from "there
    # is no drift". Measured across the fleet on 2026-08-03: ten gates behaved
    # exactly this way, and they only kept working because an earlier CI step
    # happened to install stapel-tools first. A gate that cannot run has
    # FAILED; it has not passed.
    raise RuntimeError(
        "contract drift gate cannot run: stapel-tools is not importable, and "
        "it carries the emitter this gate measures drift against. Install it "
        "(workspace venv, or `pip install stapel-tools`) and re-run. This is a "
        "hard failure on purpose — a skipped drift gate is silently no gate."
    ) from exc

from stapel_tools.surface import (  # noqa: E402
    KINDS,
    _stable_json,
    build_static_capabilities,
    load_meta,
)
from stapel_tools.llms_txt import load_inputs as load_llms_inputs  # noqa: E402
from stapel_tools.llms_txt import render as render_llms_txt  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
COMMITTED = REPO / "docs" / "capabilities.json"
COMMITTED_LLMS_TXT = REPO / "docs" / "llms.txt"


@pytest.fixture(scope="module")
def emitted() -> dict:
    try:
        return build_static_capabilities(REPO, load_meta(REPO))
    except SystemExit as exc:  # the LOUD rule — report it, don't bury it
        pytest.fail(f"capabilities emission refused: {exc}", pytrace=False)


def test_capabilities_committed():
    assert COMMITTED.is_file(), (
        "docs/capabilities.json is missing — run `make contract` and commit it"
    )


def test_no_drift(emitted):
    assert COMMITTED.read_text() == _stable_json(emitted), (
        "docs/capabilities.json is stale — run `make contract` and commit it"
    )


def test_llms_txt_committed():
    assert COMMITTED_LLMS_TXT.is_file(), (
        "docs/llms.txt is missing — run `make contract` and commit it"
    )


def test_llms_txt_has_no_drift():
    """docs/llms.txt (the fifth contract artifact) must match a fresh render of
    the committed docs/capabilities.json byte for byte."""
    rendered = render_llms_txt(load_llms_inputs(REPO))
    assert COMMITTED_LLMS_TXT.read_text() == rendered, (
        "docs/llms.txt is stale — run `make contract` and commit it"
    )


def test_llms_txt_emission_is_deterministic():
    a = render_llms_txt(load_llms_inputs(REPO))
    b = render_llms_txt(load_llms_inputs(REPO))
    assert a == b


def test_version_matches_pyproject(emitted):
    """The document carries the module version; a bump without a re-emission is
    green lint, a green local suite and one red matrix branch in CI."""
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    committed = json.loads(COMMITTED.read_text())
    assert committed["version"] == pyproject["project"]["version"]
    assert emitted["version"] == pyproject["project"]["version"]


def test_no_operations_total_without_a_schema(emitted):
    """The core serves no catalogued HTTP surface, and the document says so by
    OMITTING the counter rather than claiming a zero — an absent field is a
    limit of the format, a zero would be a claim about the module."""
    assert not (REPO / "docs" / "schema.json").exists()
    assert "operations_total" not in emitted


def test_every_surface_entry_is_explained_and_typed(emitted):
    surface = emitted["surface"]
    assert surface, "the core's whole reason to have this document is `surface`"
    for entry in surface:
        assert entry["kind"] in KINDS, entry
        assert entry["intent"].strip(), entry
        assert entry["path"], entry


def test_the_permission_classes_are_indexed(emitted):
    """`IsNotAnonymousUser` and its neighbours were indexed NOWHERE — not
    in JSON, not in YAML, not even in prose — which is how a product ended up
    writing its own gate next to a ready one."""
    names = {e["name"] for e in emitted["surface"] if e["kind"] == "permission_class"}
    assert names == {
        "IsStaffUser",
        "IsSuperUser",
        "ReadOnlyOrSuperUser",
        "ReadOnlyOrStaff",
        "IsServiceRequest",
        "IsNotAnonymousUser",
        "HasWorkspaceMandate",
    }


def test_the_three_principal_states_are_three_distinct_gates(emitted):
    """The index must not let the third state hide behind the second.

    `IsNotAnonymousUser` and `HasWorkspaceMandate` answer different questions
    — "is this a real account" and "does this account hold a mandate" — and a
    reader picking a gate off this list has to be able to see that. So the
    newer one declares the older one among the things it displaces.
    """
    entry = next(e for e in emitted["surface"] if e["name"] == "HasWorkspaceMandate")
    assert (
        "stapel_core.django.api.permissions.IsNotAnonymousUser"
        in entry["instead_of"]
    )


def test_is_not_anonymous_user_declares_what_it_displaces():
    """`instead_of` is the field the duplicate-of-surface check will run on:
    it turns "the product reached for DRF's IsAuthenticated" from a code review
    remark into something a machine can name."""
    committed = json.loads(COMMITTED.read_text())
    entry = next(
        e for e in committed["surface"] if e["name"] == "IsNotAnonymousUser"
    )
    assert "rest_framework.permissions.IsAuthenticated" in entry["instead_of"]


def test_load_configured_factors_is_surface_not_an_extension_point(emitted):
    """The seam and the call are different objects. What a host may PLUG IN is
    ``STAPEL_VERIFICATION['EXTRA_FACTORS']`` — an extension point. What somebody
    has to CALL for that setting to mean anything is ``load_configured_factors``
    — usage surface. Before 0.16.1 the seam was declared and the loader had no
    caller anywhere in the framework: the extension point was documented and
    inert. Keeping both, in their own sections, is what makes that state
    describable."""
    surface = {e["name"] for e in emitted["surface"]}
    eps = {e["name"] for e in emitted["extension_points"]}
    assert "load_configured_factors" in surface
    assert "STAPEL_VERIFICATION[\"EXTRA_FACTORS\"]" in eps
    assert "load_configured_factors" not in eps


# --- README.md — the sixth artifact ------------------------------------------
#
# README.md is assembled by ``stapel_tools.readme`` from docs/readme.md (the
# human half: what the core is and how to think about it) plus the contract
# documents above. Everything a hand-written README used to restate is
# generated: the hand-written page this replaced was titled ``stapel_core``,
# quoted no version at all, and told a reader to install the package by the
# name it had before it was published.

def test_readme_is_assembled_and_has_no_drift():
    from stapel_tools.readme import load_inputs as readme_inputs
    from stapel_tools.readme import render as render_readme
    from stapel_tools.readme import static_languages

    languages = static_languages(REPO)
    assert languages == ["en"], "expected exactly the English static body docs/readme.md"
    committed = (REPO / "README.md").read_text()
    assert committed == render_readme(REPO, readme_inputs(REPO), "en", languages), (
        "README.md drifted — run `make contract` and commit README.md "
        "(edit prose in docs/readme.md, never README.md itself)"
    )


def test_readme_version_matches_the_package():
    """The #226 gate, at the point where the number is published.

    A capabilities.json whose version lags pyproject.toml is exactly the
    defect tracked as #226; the generator refuses to render around it, so
    this test fails loudly rather than shipping a README stating a version
    the wheel does not have.
    """
    import tomllib

    from stapel_tools.readme import load_inputs as readme_inputs
    from stapel_tools.readme import resolve_version

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert resolve_version(readme_inputs(REPO)) == pyproject["project"]["version"]
