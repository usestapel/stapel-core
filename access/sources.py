"""``ROLE_SOURCES`` — the chain that answers "which roles does this user hold".

The mandate itself never assigns roles (A2); it only reads them through this
seam. ``STAPEL_ACCESS["ROLE_SOURCES"]`` is an ordered list of dotted paths
(or callables) of signature ``(user) -> list[str] | None``:

- ``None``  — "no opinion", the chain falls through to the next source;
- a list — **authoritative**, even when empty ("this user has zero roles"
  must terminate the chain, otherwise a revocation synced down into the
  local field would be silently overridden by stale ``role:*`` groups).

Default chain — the degradation ladder of admin-suite §3.7:

1. :func:`claim_roles` — the ``staff_roles`` JWT claim of the *current*
   request. AS-2 (stapel-auth transport) stamps the validated claim onto the
   request user as the transient attribute :data:`CLAIM_ATTR`; until AS-2
   lands the attribute simply never exists and the source abstains. Reading
   the live claim (not a stored copy) is what bounds revocation latency by
   the access-token lifetime (A3).
2. :func:`user_field_roles` — the local ``staff_roles`` field on the user
   model (AS-2 migration; a plain Django project can fill it by hand or
   fixture). Abstains when the model has no such attribute.
3. :func:`group_roles` — Django groups named ``role:<name>`` (works on a
   bare stapel-core project with session logins and nothing else).

With nothing configured anywhere every source abstains, the user has no
roles, and :class:`~stapel_core.access.backend.MandateBackend` grants
nothing — the mandate is opt-in by the fact of the first role (existing
projects keep today's behavior).

The resolved role set is cached per user instance (request-scoped in
practice, same pattern as ``ModelBackend``'s permission cache); a fresh
request re-evaluates the chain, so downgrades take effect immediately on
the next authentication.
"""
from __future__ import annotations

from typing import Callable, Iterable

#: Transient attribute the JWT auth layer (AS-2) sets on the request user
#: with the validated ``staff_roles`` claim value.
CLAIM_ATTR = "_stapel_staff_roles_claim"

_CACHE_ATTR = "_stapel_access_roles"


def claim_roles(user) -> list[str] | None:
    """Roles from the current request's JWT claim (see :data:`CLAIM_ATTR`)."""
    value = getattr(user, CLAIM_ATTR, None)
    if value is None:
        return None
    return [str(name) for name in value]


def user_field_roles(user) -> list[str] | None:
    """Roles from a local ``staff_roles`` attribute/field, if the model has one."""
    value = getattr(user, "staff_roles", None)
    if value is None:
        return None
    return [str(name) for name in value]


def group_roles(user) -> list[str] | None:
    """Roles from Django groups named ``role:<name>`` (session-only fallback)."""
    if user.pk is None or getattr(getattr(user, "_state", None), "adding", True):
        return None  # unsaved/anonymous user has no group memberships
    names = list(user.groups.filter(name__startswith="role:").values_list("name", flat=True))
    if not names:
        return None
    return [name[len("role:"):] for name in names]


def _resolved_sources() -> list[Callable]:
    from django.utils.module_loading import import_string

    from .conf import access_settings

    sources = []
    for entry in access_settings.ROLE_SOURCES or ():
        sources.append(import_string(entry) if isinstance(entry, str) else entry)
    return sources


def user_roles(user) -> frozenset[str]:
    """Effective role names of *user*: chain result ∩ role registry.

    Names absent from the effective role registry are dropped (forward
    compatibility — admin-suite §3.3). Cached on the user instance.
    """
    cached = getattr(user, _CACHE_ATTR, None)
    if cached is not None:
        return cached

    raw: Iterable[str] = ()
    for source in _resolved_sources():
        result = source(user)
        if result is not None:
            raw = result
            break

    from .roles import effective_roles

    registry = effective_roles()
    roles = frozenset(name for name in raw if name in registry)
    try:
        setattr(user, _CACHE_ATTR, roles)
    except AttributeError:  # exotic user objects with __slots__
        pass
    return roles


__all__ = ["CLAIM_ATTR", "claim_roles", "group_roles", "user_field_roles", "user_roles"]
