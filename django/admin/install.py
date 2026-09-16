"""Install the admin service picker into every template engine — unasked.

Why this module exists
----------------------
The cross-service picker (``admin/base_site.html`` + the ``stapel_services``
context processor + the ``STAPEL_SERVICES``/mount topology) has been built,
lost, rebuilt and lost again on live fleets since 2026-05. Every
disappearance had the same shape, and none of them was a bug in the picker:

* **2026-07-05, ironmemo.** ``iron-auth`` hand-wrote its own ``TEMPLATES``
  block instead of calling :func:`stapel_core.django.settings.
  get_common_templates`, naming the core template directory by its literal
  container path — ``/app/stapel_core/django/templates``, correct only while
  the library was bind-mounted there. The commit that moved stapel-core from
  a vendored submodule to a PyPI wheel deleted the bind mount and left the
  string. Django does not complain about a ``DIRS`` entry that does not
  exist, so the admin simply started resolving ``admin/base_site.html`` out
  of ``django.contrib.admin`` instead, and the picker was gone with no error
  anywhere. It stayed gone for two and a half months, in the one service the
  centralised admin login actually lands on.

* **2026-07-06, everywhere.** The service list moved out of the library into
  the ``STAPEL_SERVICES`` deploy-config. Deployments older than the
  generators never wrote it down, fell into the monolith fallback, and every
  admin listed only itself. (``stapel_core.nav.E004`` now catches that half.)

The common mechanism is worth naming precisely, because the obvious reading
is wrong: it is tempting to say "``APP_DIRS: True`` is on, so the template
still resolves from site-packages". **It does not.** ``APP_DIRS`` searches
app template directories in ``INSTALLED_APPS`` order, and
``django.contrib.admin`` — which ships its own ``admin/base_site.html`` —
is listed before ``stapel_core.django`` in every Stapel settings module. So
core's copy is *always* shadowed under ``APP_DIRS`` alone. The picker has
only ever rendered because some ``DIRS`` entry pointed at core's template
directory, since ``DIRS`` is searched ahead of app directories. That made
the whole feature depend on one hand-maintained path in every service's
settings — a per-service setting that nobody re-checks and that no test
asserted.

What this module does
---------------------
Removes the dependency. :func:`install_admin_nav` walks the *live*
``settings.TEMPLATES`` and, for every Django template engine, appends core's
template directory to ``DIRS`` and the nav context processor to
``OPTIONS["context_processors"]`` if they are not already there. It is
called from ``CommonDjangoConfig.ready()``, so **a service gets the picker by
installing stapel-core and nothing else** — no template, no settings line,
no opt-in, and a hand-written ``TEMPLATES`` block (iron-auth's shape) is
repaired in place without touching the service.

Ordering is deliberate: core's directory is *appended*, so a project that
genuinely ships its own ``templates/admin/base_site.html`` still wins — the
project's own ``DIRS`` entries are searched first. What core overtakes is
only ``django.contrib.admin``'s stock template, which is what was quietly
winning before.

``ready()`` is the right seam here, unlike the boot gates in
:mod:`stapel_core.django.boot`: this reads nothing from sibling apps and
decides nothing about their state — it edits one setting, idempotently.
Template engines are built lazily on first render, which under any server is
long after ``apps.populate()``; an engine that was somehow built already is
dropped so it rebuilds against the repaired setting.

The ``setting_changed`` receiver re-installs after a test swaps ``TEMPLATES``
wholesale (``override_settings``). In production that signal never fires, so
it costs nothing.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

#: The nav context processor every admin template needs in context.
NAV_CONTEXT_PROCESSOR = "stapel_core.django.admin.context.stapel_services"

#: Core's template directory, resolved from the installed package location —
#: identical for a vendored checkout and a pip-installed wheel, which is the
#: whole point (a literal container path is what rotted on ironmemo).
CORE_TEMPLATES_DIR = str(Path(__file__).resolve().parent.parent / "templates")

#: Backends whose DIRS/context_processors this module understands. A Jinja2
#: or third-party engine is left strictly alone: its DIRS mean something
#: else, and the admin does not render through it.
_DJANGO_BACKEND = "django.template.backends.django.DjangoTemplates"


def _same_path(a: Any, b: str) -> bool:
    """Path equality that survives str/Path and a trailing slash."""
    try:
        return Path(str(a)).resolve() == Path(b).resolve()
    except (OSError, ValueError):  # unresolvable entry — not our directory
        return False


def _reset_engines() -> None:
    """Drop any template engine already built from the old setting."""
    try:
        from django.template import engines
    except Exception:  # pragma: no cover — Django always importable here
        return
    try:
        engines._engines = {}
        engines._templates = None
        engines.__dict__.pop("templates", None)
    except Exception:  # pragma: no cover — defensive; never break boot
        logger.debug("admin nav: could not reset template engines", exc_info=True)


def install_admin_nav(*, reset_engines: bool = True) -> Dict[str, List[str]]:
    """Ensure every Django template engine can render the admin picker.

    Idempotent. Returns a report of what it had to repair, keyed
    ``"dirs"`` / ``"context_processors"`` with the engine names touched —
    empty lists mean the project was already wired correctly.
    """
    from django.conf import settings

    repaired: Dict[str, List[str]] = {"dirs": [], "context_processors": []}

    templates = getattr(settings, "TEMPLATES", None)
    if not templates:
        # No engine at all: the admin cannot render anything either way, and
        # inventing a whole TEMPLATES setting is far beyond this module's
        # mandate. django.contrib.admin's own admin.E403 is the finding.
        return repaired

    changed = False
    for index, engine in enumerate(templates):
        if not isinstance(engine, dict):
            continue
        if engine.get("BACKEND") != _DJANGO_BACKEND:
            continue
        name = engine.get("NAME") or f"TEMPLATES[{index}]"

        dirs = engine.get("DIRS")
        if dirs is None:
            dirs = []
            engine["DIRS"] = dirs
        if not any(_same_path(d, CORE_TEMPLATES_DIR) for d in dirs):
            # Appended, never prepended: a project's own admin override keeps
            # priority. We only need to beat django.contrib.admin's app dir.
            try:
                dirs.append(CORE_TEMPLATES_DIR)
            except AttributeError:  # a tuple — replace it
                engine["DIRS"] = [*dirs, CORE_TEMPLATES_DIR]
            repaired["dirs"].append(name)
            changed = True

        options = engine.get("OPTIONS")
        if options is None:
            options = {}
            engine["OPTIONS"] = options
        if not isinstance(options, dict):
            continue
        # A project that pins its own `loaders` opts out of DIRS entirely;
        # the context processor is still worth installing, and the
        # stapel_core.nav.E005 check reports the template it cannot reach.
        processors = options.get("context_processors")
        if processors is None:
            processors = []
            options["context_processors"] = processors
        if NAV_CONTEXT_PROCESSOR not in processors:
            try:
                processors.append(NAV_CONTEXT_PROCESSOR)
            except AttributeError:  # a tuple — replace it
                options["context_processors"] = [*processors, NAV_CONTEXT_PROCESSOR]
            repaired["context_processors"].append(name)
            changed = True

    if changed and reset_engines:
        _reset_engines()
    if changed:
        logger.debug("admin nav: repaired template config %s", repaired)
    return repaired


def _on_setting_changed(sender, setting, **kwargs):
    """Re-install after a test replaces TEMPLATES wholesale."""
    if setting == "TEMPLATES":
        install_admin_nav()


def connect_admin_nav_installer() -> None:
    """Install now, and again whenever ``TEMPLATES`` is swapped (tests)."""
    from django.test.signals import setting_changed

    install_admin_nav()
    setting_changed.connect(
        _on_setting_changed, dispatch_uid="stapel_core.admin.install_admin_nav"
    )


__all__ = [
    "CORE_TEMPLATES_DIR",
    "NAV_CONTEXT_PROCESSOR",
    "install_admin_nav",
    "connect_admin_nav_installer",
]
