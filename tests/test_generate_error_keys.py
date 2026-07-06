"""generate_error_keys management command (error-remediation task).

The command force-imports the error modules (so the artifact does not depend on
which view/serializer happened to be imported) and writes a byte-stable
errors.json — the backend companion of schema.json/flows.json.
"""
import json

from stapel_core.django.management.commands.generate_error_keys import Command


def _run(tmp_path):
    # Invoke handle() directly: the command lives in the stapel_core.django app,
    # which this core test config does not install (so call_command can't find
    # it by name), but the handler is self-contained.
    out = tmp_path / "errors.json"
    Command().handle(out=str(out))
    return out


def test_writes_sorted_valid_artifact(tmp_path):
    out = _run(tmp_path)
    entries = json.loads(out.read_text())
    assert isinstance(entries, list) and entries
    codes = [e["code"] for e in entries]
    assert codes == sorted(codes)
    for e in entries:
        assert set(e) == {"code", "status", "params", "remediation", "en"}


def test_force_imports_cross_cutting_verification_keys(tmp_path):
    # verification is a core mechanism, not a Django app — the command imports it
    # explicitly so its keys are always in the artifact.
    entries = {e["code"]: e for e in json.loads(_run(tmp_path).read_text())}
    assert "error.403.verification_required" in entries
    assert entries["error.404.verification_challenge_not_found"]["remediation"] == "verify"
    assert entries["error.423.verification_locked"]["remediation"] == "wait_and_retry"


def test_byte_stable_across_runs(tmp_path):
    a = _run(tmp_path / "a").read_bytes()
    b = _run(tmp_path / "b").read_bytes()
    assert a == b
    assert a.endswith(b"\n")
