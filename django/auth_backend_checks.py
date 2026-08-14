"""System checks for ``AUTHENTICATION_BACKENDS`` (tag ``stapel_auth_backends``).

E-level by design. The 2026-08-11 audit found a shipped backend that
resolved a user by email and returned it *without verifying the password*,
wired into a product's ``AUTHENTICATION_BACKENDS``. Every caller of
``django.contrib.auth.authenticate()`` in that process — including a legacy
token endpoint — then accepted any nonempty password for a known address.
The defect was invisible in review because the wiring and the backend live
in different repositories: the project sees a dotted path, the library sees
a class nobody is obviously calling.

The rule this check enforces is therefore about the *seam*, not about one
class: a backend that overrides ``authenticate()`` has taken over credential
verification, and must say so out loud. ``verifies_credentials = True`` is
that statement. A backend that legitimately does not verify a secret (an
identity-federation shim, a remote-user backend) is not silently wrong — it
simply may not sit in ``AUTHENTICATION_BACKENDS``, where ``authenticate()``
will hand it a password and trust its answer.

A backend that sits in ``AUTHENTICATION_BACKENDS`` only so Django will ask
it ``has_perm``/``has_module_perms`` is a different, legitimate shape — and
it has nowhere else to live, because Django resolves permissions by walking
that same list. Such a backend inherits :class:`AuthorizationOnlyBackend`
and defines no ``authenticate`` at all; the gate then exempts it
STRUCTURALLY, from the MRO, rather than trusting a declaration.

Third-party backends cannot declare the attribute, so a project that has
reviewed one lists it in
``STAPEL_SECURITY["REVIEWED_AUTH_BACKENDS"]``. That is a deliberate,
greppable, per-project decision — which is the point; the failure mode being
closed here is the one nobody ever decided.
"""
from __future__ import annotations

from django.contrib.auth.backends import BaseBackend
from django.core import checks

E001_BACKEND_UNIMPORTABLE = "stapel_core.auth_backends.E001"
E002_UNDECLARED_CREDENTIAL_HANDLING = "stapel_core.auth_backends.E002"
E003_DOES_NOT_VERIFY_CREDENTIALS = "stapel_core.auth_backends.E003"
E004_AUTHORIZATION_ONLY_OVERRIDES_AUTHENTICATE = "stapel_core.auth_backends.E004"


class AuthorizationOnlyBackend(BaseBackend):
    """Base for backends that exist only to answer ``has_perm``.

    Django resolves permissions by iterating ``AUTHENTICATION_BACKENDS``, so a
    backend that contributes only authorization has to be listed there. It is
    not an authentication path and must never return a principal.

    **This class deliberately defines no ``authenticate``.** That emptiness is
    the whole mechanism: the implementation left in force is
    ``BaseBackend.authenticate``, a no-op this library ships, and the gate
    below reads that off the MRO. A declared attribute would be a claim the
    gate has to trust — exactly the failure mode the gate exists to close.
    Giving this class an ``authenticate`` "for clarity" would destroy the
    guarantee, so a subclass that grows one is an error (E004) even if it also
    declares ``verifies_credentials``: its base class then makes a false
    machine-readable claim about it.
    """


def _overrides_authenticate(backend_cls) -> bool:
    """True if *backend_cls* supplies its own ``authenticate`` implementation.

    Compared against every ancestor Django ships rather than against
    ``ModelBackend`` alone, so a subclass that only narrows the queryset (and
    inherits Django's password check verbatim) is not dragged into the
    declaration requirement.
    """
    from django.contrib.auth.backends import (
        BaseBackend,
        ModelBackend,
        RemoteUserBackend,
    )

    if backend_cls in (BaseBackend, ModelBackend, RemoteUserBackend):
        return False

    own = backend_cls.__dict__.get("authenticate")
    if own is None:
        # Inherited unchanged from somewhere in the MRO — walk up to find
        # which implementation is actually in force.
        for ancestor in backend_cls.__mro__[1:]:
            inherited = ancestor.__dict__.get("authenticate")
            if inherited is not None:
                return ancestor not in (BaseBackend, ModelBackend, RemoteUserBackend)
        return False
    return True


@checks.register("stapel_auth_backends")
def check_authentication_backends(app_configs=None, **kwargs):
    from django.conf import settings
    from django.utils.module_loading import import_string

    from stapel_core.security.conf import security_settings

    reviewed = set(security_settings.REVIEWED_AUTH_BACKENDS or ())
    errors = []

    for path in getattr(settings, "AUTHENTICATION_BACKENDS", ()) or ():
        if not isinstance(path, str) or path in reviewed:
            continue
        try:
            backend_cls = import_string(path)
        except ImportError as exc:
            errors.append(checks.Error(
                f"AUTHENTICATION_BACKENDS entry {path!r} cannot be imported: "
                f"{exc}. Every login attempt will raise.",
                id=E001_BACKEND_UNIMPORTABLE,
            ))
            continue

        if not isinstance(backend_cls, type) or not _overrides_authenticate(backend_cls):
            continue

        if issubclass(backend_cls, AuthorizationOnlyBackend):
            # Checked before the declaration below on purpose: the class name
            # already claims "never returns a principal", so a declaration
            # must not be able to talk past a contradicting implementation.
            errors.append(checks.Error(
                f"AUTHENTICATION_BACKENDS entry {path!r} inherits "
                "AuthorizationOnlyBackend but defines its own authenticate(), "
                "so its base class now claims something untrue about it.",
                hint="Delete the authenticate() override — the inherited "
                     "no-op is what makes the exemption verifiable. If this "
                     "backend really does authenticate, stop inheriting "
                     "AuthorizationOnlyBackend and declare "
                     "verifies_credentials instead.",
                id=E004_AUTHORIZATION_ONLY_OVERRIDES_AUTHENTICATE,
            ))
            continue

        declared = getattr(backend_cls, "verifies_credentials", None)
        if declared is True:
            continue
        if declared is False:
            errors.append(checks.Error(
                f"AUTHENTICATION_BACKENDS entry {path!r} declares "
                "verifies_credentials = False, so django.contrib.auth."
                "authenticate() would accept any password it is given for a "
                "principal this backend can resolve.",
                hint="If this backend never returns a user from "
                     "authenticate(), inherit AuthorizationOnlyBackend and "
                     "delete the override — sitting here only for has_perm is "
                     "a supported shape. Otherwise remove it from "
                     "AUTHENTICATION_BACKENDS: a backend that resolves a "
                     "principal without checking a secret is not an "
                     "authentication backend; call it explicitly from the "
                     "flow that already proved the identity.",
                id=E003_DOES_NOT_VERIFY_CREDENTIALS,
            ))
            continue

        errors.append(checks.Error(
            f"AUTHENTICATION_BACKENDS entry {path!r} overrides authenticate() "
            "but does not declare whether it verifies a credential.",
            hint="Set verifies_credentials = True on the class if it calls "
                 "check_password (or an equivalent secret comparison). If it "
                 "does not, take it out of AUTHENTICATION_BACKENDS. A "
                 "third-party backend you have reviewed goes in "
                 "STAPEL_SECURITY['REVIEWED_AUTH_BACKENDS'].",
            id=E002_UNDECLARED_CREDENTIAL_HANDLING,
        ))

    return errors


__all__ = [
    "check_authentication_backends",
    "E001_BACKEND_UNIMPORTABLE",
    "E002_UNDECLARED_CREDENTIAL_HANDLING",
    "E003_DOES_NOT_VERIFY_CREDENTIALS",
]
