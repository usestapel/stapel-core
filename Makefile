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
contract:
	$(PYTHON) -m stapel_tools.surface .

# Drift gate — the authoritative CI form is tests/test_contract.py.
contract-check:
	$(PYTHON) -m stapel_tools.surface . --check
