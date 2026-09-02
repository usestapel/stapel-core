"""System check: an embedded library's static bundle that nothing can collect.

The failure this names is the quietest one the fleet has produced. A widget an
admin mounts — ``stapel_attributes``' per-kind config editor, say — is loaded
by URL from a bundle the library ships. If that bundle is not reachable by the
staticfiles finders, ``collectstatic`` never copies it, the ``<script>`` tag
resolves to a 404, and the page still renders: the form appears, saves,
validates, publishes. Only the editor is absent. Nothing is logged, nothing
raises, and the author's conclusion is that the product does not have the
feature.

:mod:`stapel_core.staticfiles` plus ``get_staticfiles_dirs()`` make this
unreachable by construction. This check exists because that is not the only
way a host writes ``STATICFILES_DIRS`` — a hand-rolled list, an overwrite
rather than an append, a service that predates the mechanism — and the
symptom is invisible in every one of those cases.

WARNING, NOT ERROR (library-standard §3.7)
------------------------------------------
The service runs. What is broken is one editor on one admin page, which is
lazy degradation by the letter of the policy, and blocking every deploy of
every service over an uncollected CSS file would be a worse trade than the
one it fixes. ``manage.py check`` prints it, ``--fail-level WARNING`` fails a
pipeline on it, and that is loud enough for a defect that is otherwise
literally invisible.

WHY IT WALKS FILES AND NOT WIDGET ``Media``
-------------------------------------------
The obvious implementation — enumerate mounted admin widgets, read their
``Media``, test each asset — would MISS the very bug it was written for.
``ConfigEditorWidget`` declares no ``Media`` at all: it calls ``static()``
inline and dynamic-imports the result from an ES module. Introspecting
``Media`` would have returned a clean report on the broken deployment. So the
check asks the question the other way round, over ground truth: every file an
embedded library ships must be findable, whoever ends up asking for it.
"""
from __future__ import annotations

import os
from typing import List

from django.core import checks

W001_EMBEDDED_STATIC_UNREACHABLE = "stapel_core.static.W001"

#: Report a few names, not a manifest. A finding that pastes 400 file paths
#: into `manage.py check` output is a finding nobody reads to the end.
_EXAMPLES = 3

#: Stop walking a single library's tree at this many files. Bundles are small;
#: a package that is not, is one whose first hundred files already answered
#: the question.
_WALK_LIMIT = 500


@checks.register(checks.Tags.staticfiles)
def check_embedded_static_collectable(app_configs=None, **kwargs) -> List[checks.Warning]:
    """W001 — a ``stapel_*`` library ships static files the finders cannot see."""
    from django.conf import settings
    from django.contrib.staticfiles import finders

    from stapel_core.staticfiles import embedded_static_packages

    installed = set(getattr(settings, "INSTALLED_APPS", ()) or ())
    findings: List[checks.Warning] = []
    for package, static_dir in embedded_static_packages():
        if package in installed:
            # A real Django app: AppDirectoriesFinder walks it. Not our class
            # of defect, and claiming it would be a false alarm on every
            # correctly-installed module in the deployment.
            continue
        missing = _unfindable(static_dir, finders)
        if missing:
            findings.append(_finding(package, static_dir, missing))
    return findings


def _unfindable(static_dir: str, finders) -> List[str]:
    """Shipped asset paths (as templates ask for them) the finders cannot resolve."""
    missing: List[str] = []
    seen = 0
    for root, _dirs, files in os.walk(static_dir):
        for filename in sorted(files):
            seen += 1
            if seen > _WALK_LIMIT:
                return missing
            relative = os.path.relpath(os.path.join(root, filename), static_dir)
            asset = relative.replace(os.sep, "/")
            try:
                found = finders.find(asset)
            except Exception:  # noqa: BLE001 - a broken finder is not our error to raise
                return []
            if not found:
                missing.append(asset)
    return missing


def _finding(package: str, static_dir: str, missing: List[str]) -> checks.Warning:
    examples = ", ".join(missing[:_EXAMPLES])
    if len(missing) > _EXAMPLES:
        examples += ", …"
    return checks.Warning(
        f"{package} ships {len(missing)} static file(s) that staticfiles "
        f"cannot find ({examples}). It is an EMBEDDED library — imported, not "
        f"listed in INSTALLED_APPS — so AppDirectoriesFinder does not walk it "
        f"and collectstatic will never copy these. Any admin widget that "
        f"mounts them renders, saves and publishes as usual, minus the widget: "
        f"the failure is invisible in the browser and silent in the logs.",
        hint=(
            "Build STATICFILES_DIRS with stapel_core.django.settings."
            "get_staticfiles_dirs(BASE_DIR), which discovers every installed "
            "stapel_* library's static/ directory — including this one — and "
            "re-run collectstatic. If this deployment pins STATICFILES_DIRS "
            f"by hand, add {static_dir!r} to it, or pass "
            "include_embedded=False to say the omission is deliberate."
        ),
        id=W001_EMBEDDED_STATIC_UNREACHABLE,
    )


__all__ = [
    "W001_EMBEDDED_STATIC_UNREACHABLE",
    "check_embedded_static_collectable",
]
