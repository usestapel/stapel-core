"""realtime-check — the fleet's realtime border, as a gate rather than a doc.

The fleet has FOUR independent realtime implementations plus an SSE proxy
(video lobby, chat, studio-dialog, runner-protocol), three of which re-invent
the same 80%: JWT auth on the socket, close codes, resume protocol, group
fan-out. None of the three browser ones is mounted by any host — the classic
seam defect, invisible to CI because every library is green in isolation. The
proliferation already happened; this checker is what stops the fifth
(stapel-realtime-design §0.5, §2.3 — the canon is "fix it in the core or with
a lint rule", never with a paragraph asking people to be careful).

The border is drawn by asking WHO is on the other side of the socket:

    A human in a browser  → `stapel-realtime` only (the substrate), and the
                            emitter is `stapel_core.comm.signal()`.
    Our own process       → not a realtime primitive at all but the transport
                            of a specific protocol (`stapel-runner-protocol`),
                            which owes an answer to "why not a Function/Task".

Rules:

- **RT001** (error) — a Channels WebSocket consumer defined outside the
  sanctioned homes: importing ``channels.generic.websocket`` or subclassing
  ``*WebsocketConsumer`` / ``AsyncConsumer`` / ``SyncConsumer``. The fifth
  hand-rolled hello/resume protocol is what this exists to prevent; use
  ``stapel-realtime``'s ``ResumableStreamConsumer`` (journal frames) or its
  ephemeral channel.
- **RT002** (error) — hand-rolled socket auth middleware: importing
  ``channels.middleware`` / ``channels.auth``, or defining the ASGI
  middleware shape ``__call__(self, scope, receive, send)``. The fleet has
  exactly one socket authentication implementation,
  ``stapel_core.django.jwt.channels`` (G14) — same provider, same blacklists,
  same close-4401-before-accept as HTTP. studio_web wrote its own; that is
  the defect, not the precedent.
- **RT003** (error) — a raw ``websockets.serve()`` server outside
  ``stapel-runner-protocol`` and its hub. Machine peer protocols are
  deliberately transport-agnostic and stay out of the browser gateway's
  lifecycle; a NEW one has to justify itself before it exists. Only the
  server: a script that *connects* to one is not a new implementation.
- **RT004** (warning) — a hand-rolled SSE endpoint: ``text/event-stream`` on
  a ``StreamingHttpResponse``. Legitimate for pass-through proxying of an
  upstream token stream (the LLM proxy), a re-invented substrate for
  anything else. SSE fallback for the substrate itself is explicitly v2.
- **RT005** (warning) — direct channel-layer fan-out (``get_channel_layer``,
  ``group_send``) instead of ``stapel_core.comm.signal()``. This is not
  stylistic: the emitter seam is what keeps the transport swappable
  (``STAPEL_COMM["SIGNAL_TRANSPORT"]``: none → channels → bus) and what lets
  an HTTP-only host run the same code with delivery switched off.

Two allowlists, and the difference between them is the whole point:

* :data:`SANCTIONED` — permanent homes. The border does not move for them.
* :data:`GRANDFATHERED` — the four implementations that predate the border,
  each pinned to the migration phase that deletes it. This list is the debt
  register: it may shrink, never grow. A new entry is a design decision, not
  a lint fix.

Suppression for a genuine one-off: append ``# realtime-check: ok — <reason>``
to the flagged line. Do NOT use it to add a fifth implementation.

Usage: ``python -m stapel_core.lint.realtime_check [PATH ...]`` (default
``.``). Skips tests, migrations, vendored trees, build artifacts and
virtualenvs. Exit code 1 on errors; warnings print and pass.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PRAGMA = "realtime-check: ok"

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    ".tox",
    ".vendor",
    "build",
    "dist",
    "node_modules",
    "__pycache__",
    "migrations",
    "tests",
}
EXCLUDED_FILES = {"conftest.py", "setup.py"}

CONSUMER_MODULES = {"channels.generic.websocket", "channels.generic"}
CONSUMER_BASES = re.compile(r"(WebsocketConsumer|AsyncConsumer|SyncConsumer)$")
SOCKET_AUTH_MODULES = {"channels.middleware", "channels.auth"}
ASGI_MIDDLEWARE_ARGS = ("self", "scope", "receive", "send")
LAYER_FANOUT_NAMES = {"get_channel_layer", "group_send"}

# Permanent homes — the border is drawn around these, not against them.
# (project name or None for "any project", path suffix, why).
SANCTIONED: tuple[tuple[str | None, str, str], ...] = (
    ("stapel-core", "django/jwt/channels.py",
     "G14 — the fleet's one socket authentication middleware"),
    (None, "studio_orchestrator/management/commands/runner_hub.py",
     "runner-protocol server: a machine peer protocol outside ASGI by design "
     "(stapel-realtime-design §2.2)"),
    (None, "studio_gateway/llm_proxy.py",
     "pass-through SSE proxying of an upstream LLM stream, not a substrate"),
)

# Whole projects that ARE the sanctioned implementations.
SANCTIONED_PROJECTS = {
    "stapel-realtime": "the browser substrate itself",
    "stapel-runner-protocol": "the machine peer protocol (design §2.2)",
}

# The debt register: what existed before the border. Each entry names the
# phase that deletes it. Shrinks only.
GRANDFATHERED: tuple[tuple[str | None, str, str], ...] = (
    ("stapel-chat", "consumers.py",
     "phase 2 — chat migrates to ResumableStreamConsumer bit-for-bit"),
    ("stapel-chat", "realtime.py",
     "phase 2 — fan-out becomes comm.signal()"),
    ("stapel-video", "consumers.py",
     "phase 2 — the lobby migrates to the ephemeral channel"),
    ("stapel-video", "realtime.py",
     "phase 2 — fan-out becomes comm.signal()"),
    (None, "svc-stapel-studio/core/asgi.py",
     "phase 3 — the fleet's only ASGI host, hand-assembled; becomes "
     "stapel-realtime's build_websocket_application()"),
    (None, "studio_web/consumers.py",
     "phase 3 — studio-dialog migrates to the substrate"),
    (None, "studio_web/auth.py",
     "phase 3 — studio's own socket JWT middleware is deleted in favour of G14"),
    (None, "studio_web/delivery.py",
     "phase 3 — fan-out becomes comm.signal()"),
)


class Finding:
    __slots__ = ("path", "line", "code", "message", "severity")

    def __init__(
        self, path: Path, line: int, code: str, message: str, severity: str = "error"
    ) -> None:
        self.path = path
        self.line = line
        self.code = code
        self.message = message
        self.severity = severity

    def __str__(self) -> str:
        prefix = "" if self.severity == "error" else f"{self.severity}: "
        return f"{self.path}:{self.line}: {prefix}{self.code} {self.message}"


_project_cache: dict[Path, str | None] = {}


def project_name(path: Path) -> str | None:
    """Distribution name from the nearest pyproject.toml above *path*.

    The allowlists are qualified by project because the fleet's layout is
    flat: a bare ``consumers.py`` suffix would grandfather every future
    module's consumers.py, which is exactly the hole this checker closes.
    """
    directory = path.parent if path.is_file() or path.suffix else path
    try:
        directory = directory.resolve()
    except OSError:
        return None
    chain: list[Path] = []
    for candidate in [directory, *directory.parents]:
        if candidate in _project_cache:
            name = _project_cache[candidate]
            break
        chain.append(candidate)
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file():
            name = _read_project_name(pyproject)
            break
    else:
        name = None
    for seen in chain:
        _project_cache[seen] = name
    return name


def _read_project_name(pyproject: Path) -> str | None:
    try:
        import tomllib

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = (data.get("project") or {}).get("name")
    return value if isinstance(value, str) else None


def _matches(entries, path: Path, project: str | None):
    # Always absolute: a suffix must mean the same thing whether the checker
    # was pointed at "." or at a full path.
    try:
        posix = path.resolve().as_posix()
    except OSError:
        posix = path.as_posix()
    for entry_project, suffix, reason in entries:
        if entry_project is not None and entry_project != project:
            continue
        if posix == suffix or posix.endswith("/" + suffix):
            return reason
    return None


def allowance(path: Path, project: str | None) -> str | None:
    """Why this file is exempt from the border, or None."""
    if project in SANCTIONED_PROJECTS:
        return SANCTIONED_PROJECTS[project]
    return _matches(SANCTIONED, path, project) or _matches(
        GRANDFATHERED, path, project
    )


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:  # relative import — never a third-party socket stack
        return []
    base = node.module or ""
    return [base] + [f"{base}.{alias.name}" for alias in node.names]


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def check_source(source: str, path: Path) -> list[Finding]:
    """AST pass over one file. Allowlisting is the caller's job (main())."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, "RT000", f"syntax error: {exc.msg}")]

    lines = source.splitlines()

    def suppressed(node: ast.AST) -> bool:
        line = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
        return PRAGMA in line

    findings: list[Finding] = []
    # A raw `serve()` is only the websockets one if this file imports the
    # library; a smoke script that merely *connects* is not a new server.
    uses_websockets = any(
        m == "websockets" or m.startswith("websockets.")
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for m in _imported_modules(n)
    )

    def report(node: ast.AST, code: str, message: str, severity: str = "error") -> None:
        if suppressed(node):
            return
        findings.append(Finding(path, node.lineno, code, message, severity))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = _imported_modules(node)
            if any(m in CONSUMER_MODULES for m in modules):
                report(node, "RT001",
                       "imports Channels consumer infrastructure — a browser "
                       "socket belongs to stapel-realtime "
                       "(ResumableStreamConsumer / the ephemeral channel), "
                       "and the emitter side is stapel_core.comm.signal()")
            if any(m in SOCKET_AUTH_MODULES for m in modules):
                report(node, "RT002",
                       "imports Channels auth/middleware — socket "
                       "authentication has exactly one home, "
                       "stapel_core.django.jwt.channels.JWTAuthMiddlewareStack "
                       "(G14): same JWT provider, blacklists and "
                       "close-4401-before-accept as HTTP")

        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if CONSUMER_BASES.search(_base_name(base)):
                    report(node, "RT001",
                           f"'{node.name}' is a hand-rolled Channels consumer "
                           f"— the fleet already has four of these and no host "
                           f"mounts three of them. Subclass "
                           f"stapel-realtime's ResumableStreamConsumer, or "
                           f"emit ephemeral frames with comm.signal()")
                    break
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "__call__"
                    and tuple(a.arg for a in item.args.args) == ASGI_MIDDLEWARE_ARGS
                ):
                    report(item, "RT002",
                           f"'{node.name}' is a hand-rolled ASGI/socket "
                           f"middleware — wrap the application with "
                           f"stapel_core.django.jwt.channels."
                           f"JWTAuthMiddlewareStack (G14) instead of "
                           f"re-implementing token handling per service")

        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in LAYER_FANOUT_NAMES:
                report(node, "RT005",
                       "talks to the channel layer directly — emit through "
                       "stapel_core.comm.signal(stream_key, type, payload) so "
                       "the transport stays an axis "
                       '(STAPEL_COMM["SIGNAL_TRANSPORT"]) and an HTTP-only '
                       "host runs the same code as a no-op",
                       severity="warning")
            elif name in ("serve", "unix_serve") and uses_websockets:
                report(node, "RT003",
                       "stands up a raw websockets server — a machine peer "
                       "protocol belongs to stapel-runner-protocol (and owes "
                       "an answer to 'why not a comm Function/Task'), a "
                       "browser socket to stapel-realtime")
            elif name == "StreamingHttpResponse" and _has_event_stream(node):
                report(node, "RT004",
                       "hand-rolled SSE endpoint — pass-through proxying of "
                       "an upstream stream is fine, anything else is a "
                       "re-invented substrate (an SSE fallback for "
                       "stapel-realtime is explicitly v2, see "
                       "stapel-realtime-design §2.3)",
                       severity="warning")

    findings.sort(key=lambda f: (f.line, f.code))
    return findings


def _has_event_stream(node: ast.Call) -> bool:
    for arg in [*node.args, *(kw.value for kw in node.keywords)]:
        if isinstance(arg, ast.Constant) and arg.value == "text/event-stream":
            return True
    return False


def iter_python_files(paths: list[Path]):
    for root in paths:
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        for p in sorted(root.rglob("*.py")):
            rel_parts = p.relative_to(root).parts
            if any(
                part in EXCLUDED_DIRS or part.endswith(".egg-info")
                for part in rel_parts[:-1]
            ):
                continue
            if p.name in EXCLUDED_FILES or p.name.startswith("test_"):
                continue
            yield p


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    roots = [Path(a) for a in args] or [Path(".")]
    findings: list[Finding] = []
    exempt: list[str] = []
    for path in iter_python_files(roots):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        file_findings = check_source(source, path)
        if not file_findings:
            continue
        reason = allowance(path, project_name(path))
        if reason is not None:
            codes = ",".join(sorted({f.code for f in file_findings}))
            exempt.append(f"{path}: {codes} allowed — {reason}")
            continue
        findings.extend(file_findings)

    findings.sort(key=lambda f: (str(f.path), f.line, f.code))
    for f in findings:
        print(f)
    for line in sorted(exempt):
        print(f"realtime-check: {line}")

    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity != "error"]
    if findings:
        print(
            f"realtime-check: {len(errors)} error(s), {len(warnings)} "
            f"warning(s). A browser socket belongs to stapel-realtime and its "
            f"emitter to stapel_core.comm.signal(); a machine peer protocol "
            f"to stapel-runner-protocol. Genuine one-off? Append "
            f"'# {PRAGMA} — <reason>' to the line.",
            file=sys.stderr,
        )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
