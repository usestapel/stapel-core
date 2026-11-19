"""Hard-dependency budget of the distribution (rule R3 ``hard-dep-budget``).

Every service in the fleet installs every hard dependency of ``stapel-core``.
The list is frozen here: adding one means editing ``BUDGET`` in the same
change, with the measured installed size and the reason it cannot be an
extra. An optional feature's third-party need goes into
``[project.optional-dependencies]`` and is imported lazily.

Compared by distribution name (normalised), not by version specifier:
raising a floor is routine, adding a distribution is the decision this gate
exists to surface.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: distribution -> why it is hard and roughly what it costs installed
#: (closure measured on CPython 3.12, macOS arm64, 2026-11).
BUDGET: dict[str, str] = {
    "django": "the framework; ~24 MB",
    "djangorestframework": "the API layer; ~2.7 MB",
    "djangorestframework-dataclasses": "dataclass serializers of the API layer",
    "drf-spectacular": "OpenAPI schema (docs/schema.json); ~1.1 MB with PyYAML",
    "django-cors-headers": "CORS middleware in COMMON_MIDDLEWARE",
    "django-redis": "the DEFAULT_CACHE backend; ~2.8 MB with redis",
    "pyjwt": "JWT verification on every request",
    "cryptography": "RS256 keys; ~12 MB with cffi",
    "requests": "comm HTTP transport, peers, monitoring; ~2.4 MB closure",
    "phonenumbers": "E.164 normalisation of the mirrored user; ~22 MB",
    "jsonschema": "comm payload validation, on by default; ~1.9 MB closure",
}


def _name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    assert match, f"unparseable requirement {requirement!r}"
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def hard_dependencies(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return {_name(req) for req in data["project"]["dependencies"]}


def budget_findings(pyproject: Path) -> list[str]:
    declared = hard_dependencies(pyproject)
    findings = [
        f"new hard dependency {name!r}: add it to BUDGET with its size and "
        "reason, or make it an extra" for name in sorted(declared - set(BUDGET))
    ]
    findings += [
        f"{name!r} is no longer a hard dependency: remove it from BUDGET"
        for name in sorted(set(BUDGET) - declared)
    ]
    return findings


def test_hard_dependencies_match_the_budget():
    assert budget_findings(REPO / "pyproject.toml") == []


def test_the_budget_saw_the_dependencies():
    assert len(hard_dependencies(REPO / "pyproject.toml")) == len(BUDGET) > 0


def test_inversion_an_added_dependency_is_red(tmp_path):
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    text = text.replace('dependencies = [\n', 'dependencies = [\n    "Pillow>=9.0",\n', 1)
    target = tmp_path / "pyproject.toml"
    target.write_text(text, encoding="utf-8")
    findings = budget_findings(target)
    assert len(findings) == 1 and "'pillow'" in findings[0]


def test_inversion_a_dropped_dependency_is_red(tmp_path):
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '    "phonenumbers>=8.13",\n' in text
    target = tmp_path / "pyproject.toml"
    target.write_text(text.replace('    "phonenumbers>=8.13",\n', ""), encoding="utf-8")
    assert budget_findings(target) == [
        "'phonenumbers' is no longer a hard dependency: remove it from BUDGET"
    ]


def test_extras_do_not_count_against_the_budget(tmp_path):
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    text = text.replace("media = [\n", 'media = [\n    "some-new-thing>=1",\n', 1)
    target = tmp_path / "pyproject.toml"
    target.write_text(text, encoding="utf-8")
    assert budget_findings(target) == []
