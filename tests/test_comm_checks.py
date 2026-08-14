"""Cross-service payload validation reports its own state at boot.

Validation is on by default now. Two ways it can be nominally on and factually
absent — no validator installed, or an explicit opt-out — and both used to be
invisible. The boot smoke names them.
"""
import sys

from django.test import override_settings

from stapel_core.comm.checks import (
    E001_VALIDATOR_MISSING,
    W002_VALIDATION_DISABLED,
    check_schema_validation,
)


def _ids(errors):
    return [e.id for e in errors]


@override_settings(STAPEL_COMM={}, DEBUG=False)
def test_default_configuration_is_clean():
    """jsonschema is a declared dependency, so the default boots silent."""
    assert check_schema_validation() == []


@override_settings(STAPEL_COMM={"VALIDATE_SCHEMAS": False})
def test_disabled_validation_is_reported():
    errors = check_schema_validation()
    assert _ids(errors) == [W002_VALIDATION_DISABLED]
    assert errors[0].level < 40  # Warning: an opt-out is the operator's to make


@override_settings(STAPEL_COMM={"VALIDATE_SCHEMAS": True})
def test_missing_validator_is_an_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "jsonschema", None)  # forces ImportError
    errors = check_schema_validation()
    assert _ids(errors) == [E001_VALIDATOR_MISSING]
    assert errors[0].level >= 40  # Error: enforcement that cannot run
