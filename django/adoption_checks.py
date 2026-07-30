"""Adoption checks (tag ``stapel_adoption``) — a third genre of system check.

The two genres that already exist answer questions about *configuration*
(``stapel_nav``: does ``STAPEL_SERVICES`` parse?) and about *topology*
(``stapel_mounts`` E004: does a module's URL surface stay inside §37?). This
one asks a different question, the one ``stapel-tools``' ``adoption_lint``
(ADO001) asks outside the process and this asks inside it:

    **the project switched an axis on — did the code that the axis affects
    actually take a position on it?**

Its idiom is the same three parts, and they are the point:

1. a **derivable premise** — a fact about the deployment nobody had to
   re-state (``AUTH_ANONYMOUS`` is on, so guest sessions exist and they are
   ``request.user.is_authenticated``);
2. a **derivable obligation** — a place where that premise makes the code
   ambiguous (a view whose only gate is a bare ``IsAuthenticated``: a guest
   passes it, and nothing in the source says whether that was wanted);
3. an **explicit waiver instead of silence** — the finding is not "close this
   view", it is "say which one you meant".

Point 3 is the whole design. A check that demanded
:class:`~stapel_core.django.api.permissions.IsNotAnonymousUser` everywhere
would be wrong on its first real consumer: in meettoday an anonymous guest
joining a call *is the product*, and several of those views must stay open.
Such a check gets silenced wholesale on day one and protects nothing. So the
red state is **silence**, never the choice. A view is green as soon as it has
chosen, by any of three routes:

* ``IsNotAnonymousUser`` in ``permission_classes`` — guests kept out, enforced;
* any other permission class alongside/instead of ``IsAuthenticated`` — a
  capability gate, an object permission, a role check: the view is gated on
  something more specific than "is logged in", so it has taken a position;
* ``stapel_anonymous_access = ANONYMOUS_ALLOWED`` (or ``ANONYMOUS_DENIED``) on
  the view — the declaration for the cases the first two cannot express.

The by-product is worth as much as the protection: after this check, "guests
may join this call" stops being an unwritten property of a permission class
that isn't there, and becomes a line of source someone can read in review.

Checks
------
E001  ``AUTH_ANONYMOUS`` is on and a view *the project wrote a gate for*
      gates on exactly a bare ``IsAuthenticated``, with no anonymous stance
      declared anywhere in its MRO.
E002  ``stapel_anonymous_access`` is present but is not one of the two
      admissible values — a typo must not read as a declaration.
W001  ``REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]`` is itself a bare
      ``IsAuthenticated`` while the axis is on. Reported once, at W-level,
      instead of once per view (see "What this does not catch").
W002  the same silence as E001, but in a view that came from an installed
      ``stapel_*`` library rather than from this project's own source.

Why W002 exists (level follows who can act)
-------------------------------------------
The E-level checks in this framework are deploy blockers: ``stapel_preflight``
lifts every Error into the deploy gate. Blocking a deploy over a line of code
that lives in somebody else's wheel — that the reader cannot edit, only wait
for — is the surest way to get the whole tag added to
``SILENCED_SYSTEM_CHECKS``, and a silenced check protects nothing. So the
level follows the reader's power to act: an Error where the source is the
project's, a Warning (naming the module, so the fix can be asked for) where it
is a library's. The library-side findings are not thereby forgiven — they are
the same finding, moving at the modules' release cadence instead of holding
their consumers' deploys hostage. Promoting W002 to E is a later, deliberate
step, once the fleet's modules have declared.

What this does not catch
------------------------
* **Views that never declared ``permission_classes``** and inherit the DRF
  project default. When that default is bare ``IsAuthenticated`` (the common
  case, and meettoday's), flagging them would mean an Error on every view in
  every installed module for a line the project never wrote — the exact flood
  that gets a check muted. Those are covered once, by W001.
* **Authorization inside the view body** — a ``get_object`` filtered by
  ownership, an early ``if request.user.is_anonymous: raise``. Such a view is
  reported even though it is safe; ``ANONYMOUS_DENIED`` is the one-line answer
  and it makes the in-body gate discoverable from the class header.
* **Whether an allowed anonymous view is *correct*.** ``ANONYMOUS_ALLOWED`` is
  a declaration of intent, not a proof; nothing here reasons about what the
  view then does with the guest.
* **Non-DRF views** (plain Django ``View``/function views): they have no
  ``permission_classes`` and their gate is a decorator this check does not
  read. ``login_required`` has exactly the same guest ambiguity, and it is
  invisible here.
"""
from __future__ import annotations

from typing import Any, Optional

from django.core import checks

E001_ANONYMOUS_STANCE_UNDECLARED = "stapel_core.adoption.E001"
E002_BAD_ANONYMOUS_DECLARATION = "stapel_core.adoption.E002"
W001_DEFAULT_GATE_IS_BARE = "stapel_core.adoption.W001"
W002_LIBRARY_STANCE_UNDECLARED = "stapel_core.adoption.W002"

_MISSING = object()

_HINT = (
    "Choose, in the view's own source: add IsNotAnonymousUser (or a "
    "capability/object permission) to permission_classes to keep guests out, "
    "or declare that guests belong here with "
    "`stapel_anonymous_access = ANONYMOUS_ALLOWED` "
    "(from stapel_core.django.api.permissions). This check reports silence, "
    "not the choice — both answers make it green."
)


def anonymous_axis_enabled() -> bool:
    """Is the ``AUTH_ANONYMOUS`` axis on in *this* deployment?

    The axis belongs to ``stapel-auth`` (``STAPEL_AUTH["AUTH_ANONYMOUS"]``,
    default ``True``), and core must not depend on it: a deployment without
    ``stapel_auth`` installed simply has no guest sessions, so the premise of
    this whole check family is false and it stays quiet. When the module *is*
    installed its own settings object is the authority (it applies the
    default and any env layering); the raw ``STAPEL_AUTH`` dict is the
    fallback for the case where importing it fails.
    """
    from django.apps import apps as django_apps

    if not django_apps.is_installed("stapel_auth"):
        return False
    try:
        from stapel_auth.conf import auth_settings

        return bool(auth_settings.AUTH_ANONYMOUS)
    except Exception:
        from django.conf import settings

        raw = getattr(settings, "STAPEL_AUTH", None) or {}
        if not isinstance(raw, dict):
            return True
        return bool(raw.get("AUTH_ANONYMOUS", True))


def _is_authenticated_class():
    try:
        from rest_framework.permissions import IsAuthenticated
    except Exception:  # pragma: no cover - DRF not installed
        return None
    return IsAuthenticated


def _declaring_class(view: type, attr: str) -> Optional[type]:
    """The class in *view*'s MRO that actually sets *attr*.

    Inheritance must not lose a gate: a project base class carrying
    ``permission_classes = [IsAuthenticated]`` for a dozen subclasses is one
    decision that the dozen views live by, and it is found here. What this
    also lets us see is *where* the value came from — a value owned by DRF
    itself is the project-wide default, not something this view said.
    """
    for klass in getattr(view, "__mro__", ()):
        if attr in vars(klass):
            return klass
    return None


def _is_drf_owned(klass: Optional[type]) -> bool:
    module = getattr(klass, "__module__", "") or ""
    return module == "rest_framework" or module.startswith("rest_framework.")


def _gate_is_bare_is_authenticated(gates) -> bool:
    """True when the view's whole permission set is exactly ``IsAuthenticated``.

    Any other class present — a capability permission, an object permission,
    a DRF ``|``/``&`` composition (which is a generated ``OperandHolder``, not
    ``IsAuthenticated``), even a project subclass of ``IsAuthenticated`` — is
    a more specific statement than "is logged in", so the view has taken a
    position and is none of this check's business.
    """
    is_authenticated = _is_authenticated_class()
    if is_authenticated is None:
        return False
    try:
        distinct = set(gates)
    except TypeError:  # pragma: no cover - unhashable exotic entries
        return False
    return distinct == {is_authenticated}


def _anonymous_declaration(view: Any):
    from stapel_core.django.api.permissions import ANONYMOUS_DECLARATION_ATTR

    return getattr(view, ANONYMOUS_DECLARATION_ATTR, _MISSING)


def library_package(view: Any) -> Optional[str]:
    """The installed ``stapel_*`` distribution a view's source belongs to, or
    ``None`` when the view is the reading project's own code.

    Deliberately the *import path* (``stapel_profiles.views`` → the
    ``stapel_profiles`` package), not
    :func:`stapel_core.django.nav.is_stapel_app`. The question here is "can
    the person reading this finding edit that file?", and the answer is
    decided by whether the file came from a wheel — a project's own local app
    that opted into the module protocol with ``stapel_module = True`` still
    lives in the project's tree and is still the project's to fix.
    """
    module = getattr(view, "__module__", "") or ""
    top = module.split(".", 1)[0]
    return top if top.startswith("stapel_") else None


def _is_api_view(view: Any) -> bool:
    try:
        from rest_framework.views import APIView
    except Exception:  # pragma: no cover - DRF not installed
        return False
    return isinstance(view, type) and issubclass(view, APIView)


@checks.register("stapel_adoption")
def check_anonymous_stance_declared(app_configs=None, **kwargs):
    """E001/E002 — with guests enabled, a bare ``IsAuthenticated`` must say
    whether it means to admit them.

    See the module docstring for why the finding is "declare a stance",
    never "close the view".
    """
    from stapel_core.django.api.permissions import (
        ANONYMOUS_DECLARATION_ATTR,
        ANONYMOUS_DECLARATIONS,
    )
    from stapel_core.django.urlsurvey import iter_surface

    if not anonymous_axis_enabled():
        return []
    if _is_authenticated_class() is None:
        return []

    findings = []
    seen: set = set()
    for entry in iter_surface():
        view = entry.view
        if not _is_api_view(view):
            continue

        owner = _declaring_class(view, "permission_classes")
        if owner is None or _is_drf_owned(owner):
            continue  # the project-wide default, not this view's statement — W001

        if not _gate_is_bare_is_authenticated(
            getattr(view, "permission_classes", ()) or ()
        ):
            continue

        key = f"{view.__module__}.{view.__qualname__}"
        if key in seen:
            continue
        seen.add(key)

        declared = _anonymous_declaration(view)
        if declared in ANONYMOUS_DECLARATIONS:
            continue

        where = f"{key} (at /{entry.full_path.lstrip('/')})"
        inherited = (
            "" if owner is view
            else f" (gate inherited from {owner.__module__}.{owner.__qualname__})"
        )
        library = library_package(view)

        if declared is not _MISSING:
            malformed = (
                f"{where} sets {ANONYMOUS_DECLARATION_ATTR} = {declared!r}, "
                f"which is not an anonymous stance. Admissible values: "
                f"{', '.join(repr(v) for v in ANONYMOUS_DECLARATIONS)}."
            )
            hint = (
                "Import the constants instead of retyping the strings: "
                "from stapel_core.django.api.permissions import "
                "ANONYMOUS_ALLOWED, ANONYMOUS_DENIED. A misspelled value must "
                "not read as a declaration — that is the one way this check "
                "could be defeated by accident."
            )
            findings.append(
                checks.Error(malformed, hint=hint,
                             id=E002_BAD_ANONYMOUS_DECLARATION)
                if library is None else
                checks.Warning(
                    malformed,
                    hint=f"{hint} The view ships in the installed "
                         f"'{library}' package — report it there.",
                    id=W002_LIBRARY_STANCE_UNDECLARED,
                )
            )
            continue

        message = (
            f"{where} is gated by a bare IsAuthenticated{inherited} while the "
            f"AUTH_ANONYMOUS axis is on. A guest session is authenticated, so "
            f"it passes this gate — and nothing in the view says whether that "
            f"is intended."
        )
        if library is None:
            findings.append(checks.Error(
                message, hint=_HINT, id=E001_ANONYMOUS_STANCE_UNDECLARED,
            ))
        else:
            findings.append(checks.Warning(
                message,
                hint=f"This view ships in the installed '{library}' package, "
                     f"so the declaration belongs in that module's own source "
                     f"— report it there rather than working around it here. "
                     f"Warning, not Error, only because a deploy must not be "
                     f"blocked on a file this project cannot edit; the "
                     f"ambiguity is the same one E001 reports.",
                id=W002_LIBRARY_STANCE_UNDECLARED,
            ))
    return findings


@checks.register("stapel_adoption")
def check_default_permission_gate(app_configs=None, **kwargs):
    """W001 — the project-wide DRF default is itself a bare ``IsAuthenticated``.

    Warning, not Error, and reported once: every view that never wrote a
    ``permission_classes`` line inherits this, and blaming each of them for a
    decision made in ``settings.py`` is how a check gets muted wholesale.
    One finding, in the one place the decision actually lives.
    """
    if not anonymous_axis_enabled():
        return []
    is_authenticated = _is_authenticated_class()
    if is_authenticated is None:
        return []
    try:
        from rest_framework.settings import api_settings

        defaults = tuple(api_settings.DEFAULT_PERMISSION_CLASSES or ())
    except Exception:  # pragma: no cover - DRF not installed/importable
        return []
    if not _gate_is_bare_is_authenticated(defaults):
        return []
    return [checks.Warning(
        "REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES'] is a bare "
        "IsAuthenticated while the AUTH_ANONYMOUS axis is on: every view that "
        "does not set permission_classes of its own admits guest sessions by "
        "default. Those views are not reported one by one (the project never "
        "wrote a line about them) — this is the single place that decision "
        "lives.",
        hint="If guests should be the exception rather than the rule, make the "
             "default IsNotAnonymousUser "
             "(stapel_core.django.api.permissions) and open the guest-facing "
             "views explicitly. If the mix is genuinely per-view, leave it and "
             "let stapel_core.adoption.E001 ask each view that gates on bare "
             "IsAuthenticated.",
        id=W001_DEFAULT_GATE_IS_BARE,
    )]


__all__ = [
    "E001_ANONYMOUS_STANCE_UNDECLARED",
    "E002_BAD_ANONYMOUS_DECLARATION",
    "W001_DEFAULT_GATE_IS_BARE",
    "W002_LIBRARY_STANCE_UNDECLARED",
    "anonymous_axis_enabled",
    "library_package",
    "check_anonymous_stance_declared",
    "check_default_permission_gate",
]
