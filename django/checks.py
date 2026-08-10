"""System checks for URL mounting (tag ``stapel_mounts``).

E-level — an auth-redirect setting (``LOGIN_URL`` / ``LOGOUT_REDIRECT_URL`` /
an explicitly set ``LOGIN_REDIRECT_URL``) that points at a path this
deployment cannot serve is a deploy blocker: it turns every ``login_required``
into a user-facing 404 *after* the redirect, which no smoke test of the page
itself catches. W-level — hints (Django's untouched stock default), never
blocking.

Paths belonging to a declared **external** mount (``STAPEL_MOUNTS`` /
``STAPEL_AUTH_SERVICE_PREFIX`` — a sibling service behind the same proxy)
cannot be verified in-process and are skipped: they are the deployment
contract, not this URLconf's business.

E004 is a different kind of mount error — surface *topology* (BACKLOG §37):
a Stapel module may only mount inside ``/<mod>/api/``, ``/<mod>/swagger/``,
``/<mod>/schema.json``, ``/<mod>/admin/``. Anything else under a module's
own prefix (a bare ``/<mod>`` root, a hand-rolled dashboard route, …) is
frontend territory — a reverse proxy that reserves the bare module prefix
for the backend silently 404s the SPA page living there.

E005/E006 are the *address* half of the same question, and they exist
because E004 could not see the failure that took app.ironmemo.com's
workspace list down for the whole life of the deployment. The host mounted
``include("stapel_workspaces.urls")`` at ``workspaces/api/workspaces/``
instead of ``workspaces/api/``, so the module's entire API answered at
``/workspaces/api/workspaces/v1/`` while every caller asked for
``/workspaces/api/v1/`` and got a 404. E004 was green throughout: it tests
for the *presence* of an ``api`` segment somewhere in the path, and that
segment was present — in the wrong place.

The obligation these two express is not "the mount literal matches a
settings constant" (that is a string guarding a string — the very shape of
the ``assert url_prefix == 'workspaces/'`` that sat directly above the
broken line and stayed green, because it compared the constant to itself
and never looked at the literal). It is:

    **a module's declared HTTP surface must be reachable at its canonical
    address**, ``/<mod>/api/v<N>/``.

A wrong mount is a wrong *connection*: the library's surface was not
unreachable by a typo, it was not plugged in — the same defect class as a
subscriber nobody starts or a registry nobody reads. Stated that way the
check covers both ends of the same axis:

* **E005** — the surface is mounted, at the wrong address (segment count
  too high, too low, or the right count in the wrong order);
* **E006** — the surface is not mounted at all, which is the same defect
  with the segment count zero, and which no literal comparison can see
  because there is no literal to compare.

Both report the address they **found** next to the address they
**expected**. A check that says only "wrong" repeats the failure it is
meant to close.

Relation to ADO001 (``stapel-tools`` ``adoption_lint``)
-------------------------------------------------------
ADO001 asks the *absence* question statically, outside the process, by
reading literal ``include("<pkg>.urls")`` pairs out of the ROOT_URLCONF
AST. It is a pre-deploy gate and it stays: it runs without a settings
module, a database, or an importable app registry.

E005/E006 ask the same family of question *inside* the process, against the
live resolver, which buys three things the AST cannot have:

* the **address**, not just the presence, of a mount — ADO001 sees that
  ``include("stapel_workspaces.urls")`` appears somewhere and is satisfied;
  only the resolver knows it landed at ``workspaces/api/workspaces/``;
* mounts ADO001 documents as opaque to it — a module included through a
  variable, a computed prefix, or an inline list nested in another include;
* what the module **contributes to its own path**, which is not symmetric
  across the fleet and is invisible in the host's source. ``stapel_agent``
  contributes ``api/v1/`` from inside its own ``urls.py``, so the correct
  host mount is ``agent/``; ``stapel_workspaces`` contributes only ``v1/``,
  so the correct host mount is ``workspaces/api/``. Two different literals,
  one canonical address. Any check that compares host literals to each
  other must get one of the two wrong.

So they are complementary rather than duplicated: ADO001 fails a project
that never wired the module, E005/E006 fail a deployment whose wiring does
not land where callers look.

What this still does not cover
------------------------------
Only the surface of the process it runs in. Each library mounts its own
URLconf correctly in its own test suite, so every library is green in
isolation and stays green; the divergence lives in the *assembled* system,
which nothing here exercises end to end. This check moves the detection
from "a user reports the app is empty" to "the service refuses to start",
which is most of the distance — but a fleet-wide end-to-end gate over the
assembled system (BACKLOG §69) remains open, and cross-service contracts
(the caller's base URL, the proxy's route table) are still unverified by
anything in this file.
"""
from __future__ import annotations

import re

from django.core import checks

E001_LOGIN_URL_UNRESOLVABLE = "stapel_core.mounts.E001"
E002_REDIRECT_URL_UNRESOLVABLE = "stapel_core.mounts.E002"
E003_BAD_MOUNTS = "stapel_core.mounts.E003"
E004_MODULE_OUTSIDE_CANON = "stapel_core.mounts.E004"
E005_MODULE_API_OFF_CANON = "stapel_core.mounts.E005"
E006_MODULE_SURFACE_UNMOUNTED = "stapel_core.mounts.E006"
W001_STOCK_LOGIN_REDIRECT = "stapel_core.mounts.W001"

#: A version segment in a module's mounted path — ``v1``, ``v2``, …
#: (api-versioning.md §2: the version sits immediately after ``api/``).
_VERSION_SEGMENT = re.compile(r"^v\d+$")

#: BACKLOG §37 canon — the only path segments a Stapel module's own URL
#: patterns may live under, anywhere in their full mounted path. Presence,
#: not position: "auth/api/v1/admin/audit/" is fine (an admin_api endpoint
#: nested *inside* the module's api/ surface), a bare "translate/dashboard/"
#: with none of these segments anywhere is not.
_CANONICAL_MODULE_SEGMENTS = {"api", "swagger", "admin", "schema", "schema.json"}

#: Django's own untouched defaults — flagged W, not E: a service that never
#: redirects there (pure API, no login_required) should not be blocked.
#: Anything *explicitly configured* that doesn't resolve is an Error.
_DJANGO_STOCK_DEFAULTS = {
    "LOGIN_URL": "/accounts/login/",
    "LOGIN_REDIRECT_URL": "/accounts/profile/",
}

_HINT = (
    "URL-target settings must survive any mount prefix: use a URL name "
    "(LOGIN_REDIRECT_URL = 'admin:index'), a lazy derivation "
    "(stapel_core.django.mounts.lazy_admin_login_url()), or declare the "
    "external service mount (STAPEL_AUTH_SERVICE_PREFIX / STAPEL_MOUNTS) "
    "instead of hardcoding a root-relative path."
)


def _external_prefixes() -> list[str]:
    from stapel_core.django.mounts import get_mounts

    return [m.prefix for m in get_mounts().values() if m.external and m.prefix]


def _strip_script_prefix(path: str) -> str:
    """Convert a browser-facing path to a URLconf path (resolve() input)."""
    from django.urls import get_script_prefix

    script_prefix = get_script_prefix()
    if script_prefix != "/" and path.startswith(script_prefix):
        return "/" + path[len(script_prefix):]
    return path


def _target_resolves(value: str) -> bool | None:
    """True/False — the target does/doesn't resolve; None — unverifiable here."""
    from django.urls import NoReverseMatch, Resolver404, resolve, reverse

    if not value:
        return None
    if "://" in value or value.startswith("//"):
        return None  # absolute URL — a cross-host contract, not our URLconf
    if not value.startswith("/"):
        # URL name / namespaced name ("admin:index") — the recommended form.
        try:
            reverse(value)
            return True
        except NoReverseMatch:
            return False
    path = _strip_script_prefix(value)
    for prefix in _external_prefixes():
        if path.startswith(f"/{prefix}"):
            return None  # another service's URL space
    try:
        resolve(path.split("?")[0])
        return True
    except Resolver404:
        return False


@checks.register("stapel_mounts")
def check_mounts_config(app_configs=None, **kwargs):
    """E003 — the STAPEL_MOUNTS merge-registry must parse."""
    from stapel_core.django.mounts import MountConfigError, get_mounts

    try:
        get_mounts()
    except MountConfigError as exc:
        return [checks.Error(
            str(exc),
            hint="Entries are {'prefix': 'auth/', 'external': True, "
                 "'namespace': ..., 'name': ...}, a prefix string, or None "
                 "to remove a builtin mount.",
            id=E003_BAD_MOUNTS,
        )]
    return []


@checks.register("stapel_mounts")
def check_auth_redirect_settings(app_configs=None, **kwargs):
    """E001/E002/W001 — LOGIN_URL & friends must point somewhere that exists."""
    from django.conf import settings

    from stapel_core.django.mounts import MountConfigError

    if not getattr(settings, "ROOT_URLCONF", ""):
        return []  # standalone package harness — nothing to resolve against

    findings = []
    targets = (
        ("LOGIN_URL", E001_LOGIN_URL_UNRESOLVABLE),
        ("LOGOUT_REDIRECT_URL", E002_REDIRECT_URL_UNRESOLVABLE),
        ("LOGIN_REDIRECT_URL", E002_REDIRECT_URL_UNRESOLVABLE),
    )
    for name, check_id in targets:
        raw = getattr(settings, name, None)
        if raw is None:
            continue
        value = str(raw)  # unwrap lazy proxies
        try:
            resolves = _target_resolves(value)
        except MountConfigError:
            continue  # E003 already reported
        if resolves is not False:
            continue
        if value == _DJANGO_STOCK_DEFAULTS.get(name):
            findings.append(checks.Warning(
                f"{name} is Django's stock default {value!r}, which this "
                "URLconf does not serve — fine if nothing redirects there, "
                "a user-facing 404 otherwise.",
                hint=_HINT,
                id=W001_STOCK_LOGIN_REDIRECT,
            ))
            continue
        findings.append(checks.Error(
            f"{name} = {value!r} does not resolve in this deployment "
            "(resolve() found no URL pattern and it matches no declared "
            "external mount) — every redirect there is a user-facing 404.",
            hint=_HINT,
            id=check_id,
        ))
    return findings


@checks.register("stapel_mounts")
def check_module_surface_containment(app_configs=None, **kwargs):
    """E004 — a Stapel module's URL patterns must stay inside its §37
    canonical sub-surfaces: ``/<mod>/api/`` (versioned inside),
    ``/<mod>/swagger/``, ``/<mod>/schema.json``, ``/<mod>/admin/``.

    A bare module root or any other suffix is frontend territory — a
    reverse proxy that reserves the whole ``/<mod>`` prefix for the backend
    (because *something* Django-side lives there) silently kills the SPA
    page at that path. That is the live incident this check exists to catch
    mechanically instead of by someone noticing the page 404 in production.

    Ownership of a URL pattern is decided the same way module discovery is
    (:func:`stapel_core.django.nav.discover_modules`): the view's
    ``__module__`` dotted-path against each installed Stapel app's
    ``AppConfig.name``. Host (non-Stapel) URLs never match any installed
    Stapel app and are silently skipped — a project is free in its own
    paths, this check is only about the modules it installed.
    """
    from stapel_core.django.urlsurvey import iter_surface, path_segments

    findings = []
    seen = set()
    for entry in iter_surface():
        app_label = entry.app_label
        if app_label is None:
            continue
        full_path = entry.full_path
        if any(seg in _CANONICAL_MODULE_SEGMENTS for seg in path_segments(full_path)):
            continue
        key = (app_label, full_path)
        if key in seen:
            continue
        seen.add(key)
        findings.append(checks.Error(
            f"Stapel module {app_label!r} mounts {full_path!r}, outside "
            "its §37 canonical sub-surfaces (no api/swagger/schema/admin "
            "segment anywhere in the path).",
            hint="A backend module may only occupy /<mod>/api/ (versioned "
                 "inside), /<mod>/swagger/, /<mod>/schema.json, "
                 "/<mod>/admin/ — move this view under one of those, or "
                 "drop it from the backend URLconf: a bare module root or "
                 "any other suffix belongs to the frontend.",
            id=E004_MODULE_OUTSIDE_CANON,
        ))
    return findings


def _headless_modules() -> set:
    """Labels/packages the project declared it runs without an HTTP surface.

    The in-process twin of ADO001's ``# stapel: headless <mod>`` marker: a
    module wanted only for its models/services/tasks. Accepts either the app
    label (``"gdpr"``) or the package (``"stapel_gdpr"``)::

        STAPEL_HEADLESS_MODULES = ["gdpr"]
    """
    from django.conf import settings

    declared = getattr(settings, "STAPEL_HEADLESS_MODULES", None) or ()
    if isinstance(declared, str):
        declared = [declared]
    out = set()
    for name in declared:
        name = str(name).strip()
        out.add(name)
        out.add(name[len("stapel_"):] if name.startswith("stapel_") else f"stapel_{name}")
    return out


def _expected_module_prefix(app_config) -> str:
    """Where this deployment says *app_config*'s surface lives, no leading
    slash, trailing slash included — ``"workspaces/"``.

    Default is the app label, which is the module slug by construction
    (``stapel_workspaces`` → ``workspaces``) and is what the canon
    ``/<mod>/api/v1/`` means by ``<mod>``. A deployment that deliberately
    hosts a module somewhere else — a co-mounted module living inside a
    sibling's prefix (``stapel_gdpr`` served by the auth service under
    ``auth/``), a renamed prefix (``sso/``) — declares that in the existing
    ``STAPEL_MOUNTS`` registry rather than being silently forgiven::

        STAPEL_MOUNTS = {"gdpr": {"prefix": "auth/"}}

    That declaration is the point, not an escape hatch: "this module is not
    at its own name" is exactly the fact a reader of the URLconf cannot
    otherwise recover, and the fact whose absence produced this incident.
    """
    from stapel_core.django.mounts import get_mount

    mount = get_mount(app_config.label)
    if mount is not None and mount.prefix:
        return mount.prefix
    return f"{app_config.label}/"


def _declares_http_surface(app_config) -> bool:
    """True when the module ships a ``urls`` submodule — the same signal
    ADO001 uses for "exposes a urlconf". Import errors are treated as *no*
    surface: a module whose urls.py cannot even be imported is a different
    (and louder) failure than a mis-mounted one.
    """
    import importlib.util

    try:
        return importlib.util.find_spec(f"{app_config.name}.urls") is not None
    except (ImportError, AttributeError, ValueError):
        return False


@checks.register("stapel_mounts")
def check_module_api_address(app_configs=None, **kwargs):
    """E005/E006 — every installed Stapel module's declared HTTP surface must
    be reachable at ``/<mod>/api/v<N>/``.

    For each installed Stapel module that ships a ``urls`` module, this walks
    the live resolver, collects the leaf paths owned by that module, and
    compares the prefix that actually precedes the version segment against
    the canonical one. Versioned leaves are the subject because they *are*
    the module's API: ``/<mod>/admin/``, ``/<mod>/swagger/`` and a host's own
    ``/<mod>/api/error-keys/`` carry no version segment and are §37 business
    (E004), not addressing business.

    Ownership is decided exactly as E004 and module discovery decide it
    (:func:`stapel_core.django.urlsurvey.callback_owner_app_label`), so a
    host's own views are never the subject of a finding and a module that is
    **not installed** has no surface to be unreachable — a service that drops
    a module reports nothing here, it does not have to remember to also drop
    an assertion about it.
    """
    from django.apps import apps as django_apps

    from stapel_core.django.mounts import MountConfigError
    from stapel_core.django.nav import is_stapel_app
    from stapel_core.django.urlsurvey import iter_surface, path_segments

    from django.conf import settings

    if not getattr(settings, "ROOT_URLCONF", ""):
        return []  # standalone package harness — no assembled surface

    try:
        headless = _headless_modules()
        subjects = {
            app_config.label: app_config
            for app_config in django_apps.get_app_configs()
            if is_stapel_app(app_config)
            and app_config.label not in headless
            and app_config.name not in headless
            and _declares_http_surface(app_config)
        }
    except MountConfigError:
        return []  # E003 already reported

    if not subjects:
        return []

    # label -> {version_prefix_found: an example full path}
    found: dict = {label: {} for label in subjects}
    mounted_at_all = {label: False for label in subjects}

    for entry in iter_surface():
        label = entry.app_label
        if label not in subjects:
            continue
        mounted_at_all[label] = True
        segments = path_segments(entry.full_path)
        for index, segment in enumerate(segments):
            if _VERSION_SEGMENT.match(segment):
                prefix = "".join(f"{seg}/" for seg in segments[:index])
                found[label].setdefault(f"{prefix}{segment}/", entry.full_path)
                break

    findings = []
    for label, app_config in sorted(subjects.items()):
        try:
            expected_prefix = _expected_module_prefix(app_config)
        except MountConfigError:
            continue  # E003 already reported
        expected_api = f"{expected_prefix}api/"

        if not mounted_at_all[label]:
            findings.append(checks.Error(
                f"Stapel module {label!r} is installed and ships "
                f"{app_config.name}.urls, but not one of its URL patterns is "
                f"reachable in this deployment — expected its API at "
                f"/{expected_api}v1/, found nothing. The module's endpoints "
                f"do not exist in this service.",
                hint=(
                    f"Mount it: path('{expected_api}', "
                    f"include('{app_config.name}.urls')) if the module "
                    f"contributes only 'v1/', or path('{expected_prefix}', ...) "
                    f"if it contributes 'api/v1/' itself — read the docstring "
                    f"of {app_config.name}.urls, the split is not the same "
                    f"across modules. If this service wants the module "
                    f"headless (models/tasks only, no HTTP), declare it: "
                    f"STAPEL_HEADLESS_MODULES = ['{label}']."
                ),
                id=E006_MODULE_SURFACE_UNMOUNTED,
            ))
            continue

        if not found[label]:
            continue  # mounted, but publishes no versioned API — E004's business

        # One finding per VERSION, not per mount. The obligation is that the
        # canonical address answers — not that nothing else does. A module
        # deliberately served at a second, legacy address as well (a
        # deprecation shim for peers pinned to an older client, which is how
        # ironmemo had to bridge this very incident) still satisfies every
        # caller built against the canon, so it is green. The red state is
        # the canonical address being ABSENT from the addresses served, which
        # is exactly what a caller experiences as a 404.
        versions: dict = {}
        for actual, example in sorted(found[label].items()):
            versions.setdefault(path_segments(actual)[-1], []).append((actual, example))

        for version, mounts in sorted(versions.items()):
            expected = f"{expected_api}{version}/"
            if any(actual == expected for actual, _ in mounts):
                continue
            actual, example = mounts[0]
            actual_segments = path_segments(actual)
            served = ", ".join(f"/{addr}" for addr, _ in mounts)
            findings.append(checks.Error(
                f"Stapel module {label!r} serves its {version} API at "
                f"{served} — none of which is the canon /{expected} "
                f"(api-versioning.md §2). Callers built against the canon get "
                f"a 404 from a service that is otherwise healthy "
                f"(example route: /{example}).",
                hint=(
                    f"Found:    {served}\n"
                    f"Expected: /{expected}\n"
                    f"Fix the host mount so the two agree — remember the "
                    f"module contributes part of this path itself "
                    f"({app_config.name}.urls says which part), so the mount "
                    f"literal is NOT the whole expected prefix. If this "
                    f"deployment deliberately hosts {label!r} somewhere other "
                    f"than /{label}/, declare it instead of moving it: "
                    f"STAPEL_MOUNTS = {{{label!r}: {{'prefix': "
                    f"{actual_segments[0] + '/'!r}}}}}."
                ),
                id=E005_MODULE_API_OFF_CANON,
            ))
    return findings


__all__ = [
    "E001_LOGIN_URL_UNRESOLVABLE",
    "E002_REDIRECT_URL_UNRESOLVABLE",
    "E003_BAD_MOUNTS",
    "E004_MODULE_OUTSIDE_CANON",
    "E005_MODULE_API_OFF_CANON",
    "E006_MODULE_SURFACE_UNMOUNTED",
    "W001_STOCK_LOGIN_REDIRECT",
    "check_auth_redirect_settings",
    "check_mounts_config",
    "check_module_api_address",
    "check_module_surface_containment",
]
