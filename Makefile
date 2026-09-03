PYTHON ?= python3

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict


# docs/capabilities.json — the module contract (capability-config.md §2).
# The core has no config axes and no OpenAPI operations, so for years it was
# the fleet's only significant library with no contract document at all: the
# format could describe what you can SWITCH ON and what you can REPLACE, and
# the core's answer to both is "nothing". What it does have is a usage
# surface — the permission classes, factories, predicates and templates a
# product is meant to call — and that is the `surface` section
# (discoverability-design.md §1.2-§1.3, stapel_tools.surface).
#
# Emitted from docs/capabilities.meta.json: the ENTRY SET is derived from the
# declared surface_roots by AST, never hand-listed, and a selected symbol with
# no curated intent line fails this target naming the symbol.
#
# docs/llms.txt — the fifth contract artifact (badge-canon §3), an agent-sized
# slice of docs/capabilities.json (stapel_tools.llms_txt). Regenerated in the
# same target so it can never drift a release behind capabilities.json.
# README.md — the sixth artifact (stapel_tools.readme). The page is ASSEMBLED,
# not written: docs/readme.md carries the human half (what the core is, how to
# think about it) and everything a hand-written README used to restate — title,
# badge row, install line, version, surface counts, doc links, licence footer —
# is generated from pyproject.toml plus the artifacts above. That is not
# theoretical here: the hand-written page this replaced still told a reader to
# `pip install -e ../iron-common-python`, the name this package had before it
# was published. Edit docs/readme.md; never README.md.
#
# --budget 6800 (0.59.0; was 6400 in 0.36.0, 4600 in 0.35.0). 0.59.0 adds the
# four monitoring/version.py entries — the "which build is this?" surface —
# which cost ~230 tokens with their intent lines already written no longer than
# their peers'. 0.36.0: the observability facade adds 25
# called symbols across four surfaces (metrics, structured logging, the error
# seam, trace correlation) plus two extension points. Raise the ceiling; do NOT
# shorten intent lines to fit — a trimmed-to-fit context file reads exactly
# like a complete one, which is the failure mode the hard budget exists to
# prevent. tests/test_contract.py carries the same number.
contract:
	$(PYTHON) -m stapel_tools.surface .
	$(PYTHON) -m stapel_tools.llms_txt . --budget 6800
	$(PYTHON) -m stapel_tools.readme .

# Drift gate — the authoritative CI form is tests/test_contract.py.
contract-check:
	$(PYTHON) -m stapel_tools.surface . --check
	$(PYTHON) -m stapel_tools.llms_txt . --check --budget 6800
	$(PYTHON) -m stapel_tools.readme . --check
