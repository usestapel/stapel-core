"""A storage root nobody can write must refuse the start, not log a warning.

The defect these pin ran in production for weeks: a shared docker volume kept
its root ownership, the service moved to uid 10001, and every media write
raised PermissionError while the boot summary said DEGRADED and carried on.
The interesting cases are therefore not "does it notice" — they are "does it
refuse", "does it stay quiet where a refusal would be wrong", and "does the
message say which directory and who owns it", because a finding an operator
cannot act on is the warning again with extra steps.
"""
import os
import sys

import pytest
from django.core import checks
from django.test import override_settings

from stapel_core.django.boot import BOOT_GATE_TAGS
from stapel_core.django.storage_checks import (
    E001_ROOT_NOT_WRITABLE,
    E002_ROOT_NOT_CREATABLE,
    E003_MALFORMED_EXTRA_ROOTS,
    W001_STORAGE_GATE_OFF,
    check_storage_roots_writable,
    configured_roots,
    probe_root,
    storage_gate_mode,
)

ENFORCING = dict(STAPEL_STORAGE_GATE="enforce")

#: The suite runs under pytest, where the gate's auto mode is deliberately
#: silent — every behavioural case therefore says which mode it is testing.
pytestmark = pytest.mark.filterwarnings("ignore:Overriding setting")


def ids_of(findings):
    return [f.id for f in findings]


def unwritable(path):
    """Take every write bit off *path* — the shape a root-owned volume has."""
    os.chmod(path, 0o555)


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
def test_unwritable_media_root_is_an_error(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    unwritable(str(media))
    try:
        with override_settings(MEDIA_ROOT=str(media), STATIC_ROOT=None, **ENFORCING):
            findings = check_storage_roots_writable()
    finally:
        os.chmod(str(media), 0o755)

    assert ids_of(findings) == [E001_ROOT_NOT_WRITABLE]
    assert findings[0].level >= checks.ERROR
    message = findings[0].msg
    assert "MEDIA_ROOT" in message
    assert str(media) in message
    # An operator has to be able to act on it: who this process is, who owns
    # the directory, and what the kernel actually said.
    assert f"uid {os.geteuid()}" in message
    assert "owned by" in message
    assert "Permission denied" in message


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
def test_a_root_that_cannot_be_created_names_its_nearest_ancestor(tmp_path):
    parent = tmp_path / "volume"
    parent.mkdir()
    unwritable(str(parent))
    missing = parent / "cdn"
    try:
        with override_settings(MEDIA_ROOT=str(missing), STATIC_ROOT=None, **ENFORCING):
            findings = check_storage_roots_writable()
    finally:
        os.chmod(str(parent), 0o755)

    assert ids_of(findings) == [E002_ROOT_NOT_CREATABLE]
    assert str(parent) in findings[0].msg


def test_a_missing_root_under_a_writable_parent_is_created(tmp_path):
    """The service creates its own root on the write path a minute later."""
    missing = tmp_path / "media" / "cdn"
    with override_settings(MEDIA_ROOT=str(missing), STATIC_ROOT=None, **ENFORCING):
        assert check_storage_roots_writable() == []
    assert missing.is_dir()


def test_a_writable_root_leaves_nothing_behind(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path), STATIC_ROOT=None, **ENFORCING):
        assert check_storage_roots_writable() == []
    assert list(tmp_path.iterdir()) == []


def test_a_root_that_is_a_file_is_reported_not_crashed(tmp_path):
    occupied = tmp_path / "media"
    occupied.write_text("not a directory")
    with override_settings(MEDIA_ROOT=str(occupied), STATIC_ROOT=None, **ENFORCING):
        findings = check_storage_roots_writable()
    assert ids_of(findings) == [E001_ROOT_NOT_WRITABLE]


# ---------------------------------------------------------------------------
# Which roots
# ---------------------------------------------------------------------------

def test_static_media_log_and_extra_roots_are_all_asked(tmp_path):
    logfile = tmp_path / "logs" / "app.log"
    with override_settings(
        MEDIA_ROOT=str(tmp_path / "media"),
        STATIC_ROOT=str(tmp_path / "static"),
        LOGGING={"handlers": {"file": {"filename": str(logfile)}}},
        STAPEL_STORAGE_ROOTS={"EXPORTS": str(tmp_path / "exports")},
    ):
        roots = dict((path, label) for label, path in configured_roots())

    assert roots[str(tmp_path / "media")] == "MEDIA_ROOT"
    assert roots[str(tmp_path / "static")] == "STATIC_ROOT"
    assert roots[str(tmp_path / "logs")] == "LOGGING['handlers']['file']['filename']"
    assert roots[str(tmp_path / "exports")] == "EXPORTS"


def test_one_path_under_two_names_is_probed_once(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path), STATIC_ROOT=str(tmp_path)):
        roots = configured_roots()
    assert roots == [("MEDIA_ROOT / STATIC_ROOT", str(tmp_path))]


def test_an_unset_root_is_not_invented():
    """Django ships MEDIA_ROOT='' and STATIC_ROOT=None; neither is a directory."""
    with override_settings(MEDIA_ROOT="", STATIC_ROOT=None, LOGGING={}):
        assert configured_roots() == []


def test_a_console_only_logging_config_contributes_nothing():
    logging = {"handlers": {"console": {"class": "logging.StreamHandler"}}}
    with override_settings(MEDIA_ROOT="", STATIC_ROOT=None, LOGGING=logging):
        assert configured_roots() == []


def test_malformed_extra_roots_is_an_error():
    with override_settings(STAPEL_STORAGE_ROOTS="/app/exports", **ENFORCING):
        findings = check_storage_roots_writable()
    assert E003_MALFORMED_EXTRA_ROOTS in ids_of(findings)


# ---------------------------------------------------------------------------
# Modes — a gate that fires where it is wrong gets silenced where it is right
# ---------------------------------------------------------------------------

def test_auto_is_silent_under_a_test_runner():
    assert "pytest" in sys.modules
    assert storage_gate_mode() == "off"


def test_auto_warns_under_debug(monkeypatch):
    monkeypatch.setattr(
        "stapel_core.django.storage_checks._under_test_runner", lambda: False
    )
    with override_settings(DEBUG=True):
        assert storage_gate_mode() == "warn"


def test_auto_enforces_in_a_deployment(monkeypatch):
    monkeypatch.setattr(
        "stapel_core.django.storage_checks._under_test_runner", lambda: False
    )
    with override_settings(DEBUG=False):
        assert storage_gate_mode() == "enforce"


def test_an_unreadable_mode_does_not_open_the_gate(monkeypatch):
    monkeypatch.setattr(
        "stapel_core.django.storage_checks._under_test_runner", lambda: False
    )
    with override_settings(STAPEL_STORAGE_GATE="enfroce", DEBUG=False):
        assert storage_gate_mode() == "enforce"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
def test_warn_mode_reports_the_same_id_at_a_lower_level(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    unwritable(str(media))
    try:
        with override_settings(
            MEDIA_ROOT=str(media), STATIC_ROOT=None, STAPEL_STORAGE_GATE="warn"
        ):
            findings = check_storage_roots_writable()
    finally:
        os.chmod(str(media), 0o755)

    assert ids_of(findings) == [E001_ROOT_NOT_WRITABLE]
    assert findings[0].level == checks.WARNING


def test_off_says_so_instead_of_going_quiet(tmp_path):
    with override_settings(MEDIA_ROOT=str(tmp_path), STAPEL_STORAGE_GATE="off"):
        findings = check_storage_roots_writable()
    assert ids_of(findings) == [W001_STORAGE_GATE_OFF]


# ---------------------------------------------------------------------------
# Where it runs
# ---------------------------------------------------------------------------

def test_the_tag_is_on_the_boot_gate_roster():
    """Under gunicorn nothing else runs checks — and gunicorn does the writing."""
    assert "stapel_storage" in BOOT_GATE_TAGS


def test_the_check_is_registered_under_its_tag():
    registered = [
        check for check in checks.registry.registry.get_checks()
        if "stapel_storage" in getattr(check, "tags", ())
    ]
    assert check_storage_roots_writable in registered


def test_probe_root_is_idempotent(tmp_path):
    assert probe_root(str(tmp_path)) is None
    assert probe_root(str(tmp_path)) is None
    assert list(tmp_path.iterdir()) == []
