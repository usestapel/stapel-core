"""System check: a storage root the service cannot write (tag ``stapel_storage``).

THE DEFECT THIS NAMES
---------------------
A shared docker named volume keeps the ownership of whoever wrote into it
first. Every fleet in this workspace created those volumes while its
containers still ran as root, and then moved the containers onto the Stapel
base images, which end ``USER stapel`` (uid 10001). The trees are mode
755/644, so the new uid keeps READING everything and can write nothing — and
nothing about that is visible until a user-facing write path touches it.

Lived example (a client fleet, found 2026-09-15 after weeks in production):
``/app/media`` was root-owned, so ``stapel_cdn.import_from_url`` died with
``PermissionError: [Errno 13] ... '/app/media/cdn/avatar/<hash>'`` on every
single avatar import, and every service start logged one ``collectstatic``
PermissionError line and carried on. The API answered 2xx, the log line was a
``DEGRADED`` in a bootstrap summary nobody reads twice, and the feature was
simply absent. The volume had been handed over for one service and not the
rest; there was no moment at which anything refused.

WHY AN ERROR, AND NOT A WARNING
-------------------------------
Because the warning is what let it run for weeks. A service whose media root
is unwritable is not degraded, it is broken for every write path it has, and
the only honest thing it can do is refuse to start and say which directory.
``manage.py`` refuses on the finding; ``stapel_core.django.boot`` carries the
tag on its roster, so a gunicorn worker refuses too (``BOOT_GATE_TAGS``).

WHAT IT CHECKS, AND WHAT IT WILL NOT
------------------------------------
Only roots this deployment CONFIGURED: ``MEDIA_ROOT``, ``STATIC_ROOT``, the
directory of every ``LOGGING`` handler that writes to a file, and anything a
host adds through :data:`EXTRA_ROOTS_SETTING`. It reads no volume list and
knows nothing about docker: the question it asks is the one the runtime asks
— *can this process create a file here* — so it is equally true of a bind
mount, a named volume, a read-only filesystem and a full disk.

A missing root is CREATED (``exist_ok``), because a service that cannot
create its own root is precisely the failure being hunted, and there is no
way to ask the question about a tree that does not exist yet. That is also
what ``collectstatic`` and ``FileSystemStorage`` do on the write path a
minute later.

MODES (:data:`GATE_SETTING`)
---------------------------
``auto`` (default) — ``enforce`` in a deployment, ``warn`` under ``DEBUG``,
silent under a test runner. A library's own suite configures throwaway roots
and a laptop configures container paths it has no business creating; a gate
that goes off there is a gate that gets silenced fleet-wide in a week.
``enforce`` / ``warn`` / ``off`` say it outright; ``off`` reports W001, so a
disabled gate is a stated choice rather than a quiet one. A finding keeps its
``E`` id under ``warn``: the id names the defect, not how loudly this
deployment happens to be hearing about it.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from django.core import checks

E001_ROOT_NOT_WRITABLE = "stapel_core.storage.E001"
E002_ROOT_NOT_CREATABLE = "stapel_core.storage.E002"
E003_MALFORMED_EXTRA_ROOTS = "stapel_core.storage.E003"
W001_STORAGE_GATE_OFF = "stapel_core.storage.W001"

#: ``auto`` | ``enforce`` | ``warn`` | ``off``.
GATE_SETTING = "STAPEL_STORAGE_GATE"

#: Extra roots a host writes to that Django does not name. Either a sequence
#: of paths, or ``{label: path}`` — the label is what the finding calls it.
EXTRA_ROOTS_SETTING = "STAPEL_STORAGE_ROOTS"

AUTO, ENFORCE, WARN, OFF = "auto", "enforce", "warn", "off"

_PROBE_PREFIX = ".stapel-writable-"

_HINT = (
    "This is ownership, not configuration: a shared docker volume keeps the "
    "uid that first wrote into it, so a service that moved onto the Stapel "
    "base images (USER stapel, uid 10001) inherits a tree it can only read. "
    "Hand the service its own subtree BEFORE it starts — a deploy-time "
    "chown-service-volumes step, run after the sync and before `up -d` — "
    "rather than putting the container back on root. If this root is "
    "genuinely meant to be read-only for this process, stop configuring it "
    "as a storage root."
)


def _under_test_runner() -> bool:
    """Is this process a test run rather than a deployment?

    Same signal as :mod:`stapel_core.django.prodguard`, and for the same
    reason: a suite's throwaway roots are correct for it, and enforcing there
    turns every library's CI red over a value that was never a deployment.
    """
    import sys

    return "pytest" in sys.modules or sys.argv[1:2] == ["test"]


def _raw_gate_value() -> str:
    from django.conf import settings

    return str(getattr(settings, GATE_SETTING, AUTO) or AUTO).strip().lower()


def storage_gate_mode() -> str:
    """``"enforce"`` | ``"warn"`` | ``"off"``, after resolving :data:`GATE_SETTING`.

    An unreadable value means ``auto``, never ``off``: a typo in the switch
    must not be the thing that disables the gate.
    """
    from django.conf import settings

    raw = _raw_gate_value()
    if raw in (ENFORCE, WARN, OFF):
        return raw
    if _under_test_runner():
        return OFF
    if getattr(settings, "DEBUG", False):
        return WARN
    return ENFORCE


def _logging_file_roots() -> List[Tuple[str, str]]:
    """Directories named by ``LOGGING`` handlers that write to a file."""
    from django.conf import settings

    config = getattr(settings, "LOGGING", None)
    if not isinstance(config, dict):
        return []
    handlers = config.get("handlers")
    if not isinstance(handlers, dict):
        return []
    roots: List[Tuple[str, str]] = []
    for name, handler in handlers.items():
        if not isinstance(handler, dict):
            continue
        filename = handler.get("filename")
        if not filename:
            continue
        directory = os.path.dirname(os.fspath(filename)) or "."
        roots.append((f"LOGGING['handlers']['{name}']['filename']", directory))
    return roots


def _extra_roots() -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """(roots, error) from :data:`EXTRA_ROOTS_SETTING`."""
    from django.conf import settings

    raw = getattr(settings, EXTRA_ROOTS_SETTING, None)
    if not raw:
        return [], None
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, (list, tuple, set)):
        items = [(f"{EXTRA_ROOTS_SETTING}[{path!r}]", path) for path in raw]
    else:
        return [], (
            f"{EXTRA_ROOTS_SETTING} is {type(raw).__name__}; it must be a "
            f"sequence of paths or a {{label: path}} mapping."
        )
    roots: List[Tuple[str, str]] = []
    for label, path in items:
        if not path or not isinstance(path, (str, os.PathLike)):
            return [], (
                f"{EXTRA_ROOTS_SETTING} entry {label!r} is {path!r}; every "
                f"entry must be a non-empty filesystem path."
            )
        roots.append((str(label), os.fspath(path)))
    return roots, None


def configured_roots() -> List[Tuple[str, str]]:
    """``[(label, absolute path)]`` — every storage root this service declares.

    Deduplicated by absolute path, so a service whose media and static roots
    coincide is reported once, under both names.
    """
    from django.conf import settings

    declared: List[Tuple[str, str]] = []
    for name in ("MEDIA_ROOT", "STATIC_ROOT"):
        value = getattr(settings, name, None)
        if value:
            declared.append((name, os.fspath(value)))
    declared.extend(_logging_file_roots())
    extra, _ = _extra_roots()
    declared.extend(extra)

    merged: Dict[str, List[str]] = {}
    for label, path in declared:
        absolute = os.path.abspath(path)
        merged.setdefault(absolute, [])
        if label not in merged[absolute]:
            merged[absolute].append(label)
    return [(" / ".join(labels), path) for path, labels in merged.items()]


def _process_identity() -> str:
    getuid = getattr(os, "geteuid", None)
    getgid = getattr(os, "getegid", None)
    if getuid is None or getgid is None:  # pragma: no cover - non-POSIX
        return "this process"
    return f"uid {getuid()}:{getgid()}"


def _describe(path: str) -> str:
    """``owner 0:0, mode 0755`` for *path*, or the reason it cannot be said."""
    try:
        info = os.stat(path)
    except OSError as exc:
        return f"cannot be stat'ed ({exc.strerror})"
    return f"owned by {info.st_uid}:{info.st_gid}, mode {info.st_mode & 0o7777:04o}"


def _nearest_existing(path: str) -> str:
    """The closest ancestor of *path* that exists — whose mode is the answer."""
    current = os.path.abspath(path)
    while True:
        if os.path.exists(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return current
        current = parent


def probe_root(path: str) -> Optional[Tuple[str, str]]:
    """``(check id, message)`` when *path* is unusable, else ``None``.

    Creates *path* when it is missing, then creates and removes one probe
    file in it. Nothing else is written, and the probe is removed on every
    exit path including failure.
    """
    if not os.path.isdir(path):
        if os.path.exists(path):
            return (
                E001_ROOT_NOT_WRITABLE,
                f"{path!r} exists and is not a directory ({_describe(path)}).",
            )
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            ancestor = _nearest_existing(path)
            return (
                E002_ROOT_NOT_CREATABLE,
                f"{path!r} does not exist and cannot be created by "
                f"{_process_identity()}: {exc.strerror} (errno {exc.errno}). "
                f"Its nearest existing ancestor {ancestor!r} is "
                f"{_describe(ancestor)}.",
            )

    probe = os.path.join(path, f"{_PROBE_PREFIX}{os.getpid()}")
    try:
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        return (
            E001_ROOT_NOT_WRITABLE,
            f"{path!r} is not writable by {_process_identity()}: "
            f"{exc.strerror} (errno {exc.errno}). The directory is "
            f"{_describe(path)}.",
        )
    try:
        os.close(descriptor)
    finally:
        try:
            os.unlink(probe)
        except OSError:  # pragma: no cover - a root that took the file back
            pass
    return None


@checks.register("stapel_storage")
def check_storage_roots_writable(app_configs=None, **kwargs):
    """E001/E002/E003/W001 — every configured storage root must be writable."""
    mode = storage_gate_mode()
    findings: List[checks.CheckMessage] = []

    _, malformed = _extra_roots()
    if malformed:
        findings.append(checks.Error(
            malformed,
            hint=f"{EXTRA_ROOTS_SETTING} = ['/app/exports'] or "
                 f"{{'EXPORTS_DIR': '/app/exports'}}.",
            id=E003_MALFORMED_EXTRA_ROOTS,
        ))

    if mode == OFF:
        if _raw_gate_value() != OFF:
            # Auto decided this is a test run. Reporting that on every
            # `manage.py check` of every service would put a permanent first
            # line above every other finding — which is how a check registry
            # stops being read. A STATED off still reports, below.
            return findings
        findings.append(checks.Warning(
            f"{GATE_SETTING} is off: storage roots are not probed on this "
            f"deployment. An unwritable MEDIA_ROOT/STATIC_ROOT fails every "
            f"write path at runtime instead of refusing the start.",
            hint=f"Remove {GATE_SETTING} to return to auto (enforce in a "
                 f"deployment, warn under DEBUG, silent under a test runner).",
            id=W001_STORAGE_GATE_OFF,
        ))
        return findings

    level = checks.Warning if mode == WARN else checks.Error
    for label, path in configured_roots():
        problem = probe_root(path)
        if problem is None:
            continue
        check_id, message = problem
        findings.append(level(f"{label}: {message}", hint=_HINT, id=check_id))
    return findings
