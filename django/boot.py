"""Boot gates that actually run in production.

Django runs system checks for management commands and ``runserver``. It runs
none for ``gunicorn config.wsgi:application`` — and that is exactly how every
generated Stapel project boots (stapel-tools emits
``CMD ["gunicorn", "config.wsgi:application", ...]`` and a bare
``get_wsgi_application()``). So the 0.24.0 E-gate wave — the CORS
allow-all-with-credentials refusal, the auth-backend refusal, the rest —
guarded developer laptops and CI and possibly guarded production not at all.
A gate that only fires where the damage is cheap is decoration.

The seam Django leaves open is middleware construction.
``get_wsgi_application()`` builds a ``WSGIHandler``, whose ``__init__`` calls
``load_middleware()``, which instantiates every middleware in ``MIDDLEWARE``.
Under gunicorn that happens at worker boot, before the first request. A
middleware ``__init__`` that raises ``ImproperlyConfigured`` therefore refuses
the worker the same way ``manage.py`` refuses today — at server construction,
not at import, which is what a boot gate is. ASGI/channels take the same path.

Why not ``AppConfig.ready()``, the obvious-looking alternative:

1. ``apps.populate()`` ordering — sibling apps' ``ready()`` hooks may not have
   run, so a check could read half-built state and report a finding that is
   not real.
2. It would crash ``manage.py check`` itself during setup, so the tool whose
   whole job is printing the diagnosis could never print it. That is a gate
   lying about its cause, structurally.
3. It breaks ``shell`` and ``migrate`` on the very box an operator is using to
   debug the finding.

Django runs checks after setup, per command, for exactly those reasons. This
module does not relitigate that; it joins the WSGI boot path Django left open.

On success the middleware raises ``MiddlewareNotUsed``: Django unhooks it from
the chain, so the per-request cost is zero — the gate runs once per worker.
"""
from __future__ import annotations

import logging

from django.core import checks

logger = logging.getLogger(__name__)

#: Check tags run at worker boot.
#:
#: The roster is an explicit ALLOWLIST, never "everything registered", and the
#: admission rule is: settings-only and DB-free. A boot gate that needs the
#: database up is not a config gate, it is a liveness probe wearing one —
#: it would turn "Postgres is 3 seconds behind the app" into "the fleet
#: refuses to start". DB-touching checks stay in ``stapel_preflight``.
#:
#: Deliberately EXCLUDED, and why, so the next person does not "helpfully"
#: add them back: ``stapel_mounts``/``stapel_nav``/``stapel_admin`` resolve
#: URLconfs and read the admin registry — loading the URLconf from inside
#: ``load_middleware()`` is a re-entrancy trap; ``stapel_access``,
#: ``stapel_adoption`` and ``stapel_cdn`` inspect models and views;
#: ``stapel_netintel``, ``stapel_secrets``, ``stapel_templates`` and
#: ``stapel_blacklist`` are W-only, so they would never refuse a boot anyway
#: and are better read from ``manage.py check``.
BOOT_GATE_TAGS: tuple[str, ...] = (
    # E: a backend that overrides authenticate() without declaring that it
    # verifies a credential — the shape that turned a password login into
    # "any nonempty string".
    "stapel_auth_backends",
    # E: allow-all origins combined with credentials — django-cors-headers
    # then reflects the caller's Origin.
    "stapel_cors",
    # W: an env var that is set and silently ignored. W-only, so it never
    # refuses a boot; it rides along because a worker boot is the moment an
    # operator's belief about the environment is most likely to be wrong.
    "stapel_conf",
    # E: schema validation is on but jsonschema is not importable.
    "stapel_comm",
    # E: the configured bus backend's client library is not installed.
    "stapel_bus",
    # E: a CONFIG.MD-declared required key with no value and no default.
    "stapel_config",
    # E: a captcha backend is named but cannot be built — the shape that
    # silently degraded to "pass every token".
    "stapel_captcha",
)

#: ``"enforce"`` (default) | ``"warn"`` | ``"off"``.
BOOT_GATES_SETTING = "STAPEL_BOOT_GATES"
ENFORCE, WARN, OFF = "enforce", "warn", "off"

W001_BOOT_GATES_NOT_ENFORCED = "stapel_core.boot.W001"
W002_BOOT_GATE_MIDDLEWARE_MISSING = "stapel_core.boot.W002"

MIDDLEWARE_PATH = "stapel_core.django.boot.BootGateMiddleware"


def boot_gate_mode() -> str:
    """Read the mode, normalised. An unreadable value means enforce.

    Failing to parse the switch must not open the gate: a typo in
    ``STAPEL_BOOT_GATES`` is exactly the moment someone would otherwise ship a
    silently ungated fleet.
    """
    from django.conf import settings

    raw = getattr(settings, BOOT_GATES_SETTING, ENFORCE)
    mode = str(raw).strip().lower()
    return mode if mode in (ENFORCE, WARN, OFF) else ENFORCE


def run_boot_gates() -> list:
    """Run the allowlisted checks and return every ERROR-or-worse finding.

    Split out from the middleware so the roster is testable without building a
    WSGI handler, and so a caller (preflight, a test) can ask "what would the
    boot gate say?" without the boot gate's consequences.
    """
    findings = checks.run_checks(tags=list(BOOT_GATE_TAGS))
    return [f for f in findings if f.level >= checks.ERROR]


def format_findings(findings) -> str:
    """Every finding, verbatim, in one refusal.

    All of them, not the first: an operator who has to redeploy to discover
    the second misconfiguration learns to distrust the gate. Message and hint
    are reproduced as the check wrote them — the gate names its causes and
    never paraphrases them.
    """
    lines = [
        f"{len(findings)} configuration error(s) refuse this worker "
        f"({BOOT_GATES_SETTING}={ENFORCE!r}):"
    ]
    for finding in findings:
        lines.append(f"  [{finding.id}] {finding.msg}")
        if finding.hint:
            lines.append(f"      HINT: {finding.hint}")
    lines.append(
        f"Run `manage.py stapel_preflight` (or `manage.py check`) to see the "
        f"same findings before deploying. Set {BOOT_GATES_SETTING}='warn' to "
        f"log instead of refusing — a stated, check-reported choice."
    )
    return "\n".join(lines)


class BootGateMiddleware:
    """Refuse a worker whose configuration an E-gate rejects.

    Placed at index 0 of ``COMMON_MIDDLEWARE`` so every project that
    instantiates that list is covered on its next core bump, with no
    entrypoint edit anywhere in the fleet.

    This middleware never handles a request. Either it raises, or it raises
    ``MiddlewareNotUsed`` and Django drops it from the chain.
    """

    def __init__(self, get_response):
        from django.core.exceptions import ImproperlyConfigured, MiddlewareNotUsed

        mode = boot_gate_mode()
        if mode == OFF:
            # Silent here on purpose — the W-check is what says it out loud,
            # in the one place an operator is already reading findings.
            raise MiddlewareNotUsed()

        findings = run_boot_gates()
        if findings and mode == ENFORCE:
            raise ImproperlyConfigured(format_findings(findings))
        if findings:
            logger.error(
                "%s=%r: booting with %d configuration error(s) that would "
                "otherwise refuse this worker.\n%s",
                BOOT_GATES_SETTING, WARN, len(findings), format_findings(findings),
            )

        raise MiddlewareNotUsed()

    def __call__(self, request):  # pragma: no cover - unreachable by construction
        raise AssertionError(
            "BootGateMiddleware always raises MiddlewareNotUsed in __init__"
        )


@checks.register("stapel_boot")
def check_boot_gates_enforced(app_configs=None, **kwargs):
    """W001 — the boot gate is not enforcing.

    Same idiom as ``STAPEL_BLACKLIST_FAIL_OPEN``: turning a gate down is a
    legitimate stance during an incident, and a stance that becomes forgotten
    configuration is how the next incident happens. So it reports itself at
    every boot smoke.
    """
    mode = boot_gate_mode()
    if mode == ENFORCE:
        return []
    return [checks.Warning(
        f"{BOOT_GATES_SETTING}={mode!r}: configuration errors that would "
        f"refuse a worker "
        + ("are logged and ignored." if mode == WARN else "are not even checked.")
        + " Under gunicorn nothing else runs these checks, so this service can "
          "start on a configuration its own gates reject.",
        hint=f"Remove {BOOT_GATES_SETTING} (or set it to 'enforce', the "
             f"default) once the findings are fixed. `manage.py check` and "
             f"`manage.py stapel_preflight` list them.",
        id=W001_BOOT_GATES_NOT_ENFORCED,
    )]


@checks.register("stapel_boot")
def check_boot_gate_middleware_installed(app_configs=None, **kwargs):
    """W002 — a hand-rolled MIDDLEWARE that never picked up the gate.

    Projects reusing ``COMMON_MIDDLEWARE`` get this by construction; projects
    that spelled their own list get nothing and would never know. Visible in
    CI and ``manage.py check``; stapel-tools' adoption_lint carries the
    fleet-sweepable error form of the same condition.
    """
    from django.conf import settings

    middleware = list(getattr(settings, "MIDDLEWARE", None) or ())
    if MIDDLEWARE_PATH in middleware:
        return []
    return [checks.Warning(
        "MIDDLEWARE does not include BootGateMiddleware, so this project's "
        "system checks do not run under gunicorn/uvicorn — only under "
        "manage.py. A configuration its own E-gates reject will boot in "
        "production and be caught nowhere.",
        hint=f"Add {MIDDLEWARE_PATH!r} as the FIRST entry of MIDDLEWARE, or "
             f"build MIDDLEWARE from stapel_core.django.settings."
             f"COMMON_MIDDLEWARE, which already contains it.",
        id=W002_BOOT_GATE_MIDDLEWARE_MISSING,
    )]


__all__ = [
    "BOOT_GATE_TAGS",
    "BOOT_GATES_SETTING",
    "BootGateMiddleware",
    "MIDDLEWARE_PATH",
    "W001_BOOT_GATES_NOT_ENFORCED",
    "W002_BOOT_GATE_MIDDLEWARE_MISSING",
    "boot_gate_mode",
    "format_findings",
    "check_boot_gate_middleware_installed",
    "check_boot_gates_enforced",
    "run_boot_gates",
]
