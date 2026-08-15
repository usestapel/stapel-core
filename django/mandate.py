"""The third principal state: authenticated, holding no mandate anywhere.

The fleet's authorization vocabulary has two words — anonymous and
authenticated — and views reason as if the second were sufficient. It is not.
A registered account with no accepted, unsuspended membership in any workspace
is neither: it is a **guest**, and stapel-workspaces has said so since the
mandate-model vardict (``permissions.is_guest`` /
``permissions.has_active_mandate``). That predicate had zero consumers outside
its own package, and in a split deployment a sibling module could not reach it
at all: the workspaces comm surface publishes ``check_membership`` and
``check_capability``, both workspace-scoped, and neither answers "does this
user hold a mandate ANYWHERE".

This module is that missing word, in the one place every module already
depends on. Three states, three distinguishable answers
(:class:`MandateState`), plus a fourth outcome that is not an answer at all:

    ANONYMOUS  — no session, or a session Django does not consider authenticated
    GUEST      — a real account with no active mandate in any workspace
    MANDATED   — at least one accepted, unsuspended membership somewhere
    (raises)   — the question could not be asked

The fourth is the point. A failed lookup is not a negative answer, and for an
authorization question the safe degradation is refusal, not admission: this
module never turns "workspaces is unreachable" into ``GUEST`` (which a caller
would render as a 403 and an operator would read as a verdict about the user).
It raises :class:`MandateLookupUnavailable`, and
:class:`~stapel_core.django.api.permissions.HasWorkspaceMandate` turns that
into an honest 503.

How the question travels
------------------------
1. the comm Function :data:`MANDATE_FUNCTION` — the declared seam, and the
   only one that works when the caller does not embed workspaces. Reachability
   is decided by ``comm.function_unreachable_reason``, never by reading the
   route table, so it is right for every transport;
2. failing that, the in-process predicate, when ``stapel_workspaces`` happens
   to be installed here (the monolith). Same answer, no hop — and it means a
   monolith gets the third state today, without waiting for the provider half
   of the seam to ship;
3. failing both, :class:`MandateLookupUnavailable`, logged at ERROR. A
   deployment wired for neither refuses every mandated view loudly instead of
   admitting everyone quietly, and ``stapel_core.mandate.E001`` says so at
   ``manage.py check`` / ``stapel_preflight`` — before the deploy, not after
   the first 503.

The provider half of :data:`MANDATE_FUNCTION` belongs in stapel-workspaces,
next to ``check_membership``/``check_capability``: it reads workspace tables,
which is that module's business and not the core's. :data:`MANDATE_SCHEMA`
and :data:`MANDATE_RESULT_KEY` are the contract it implements.

Caching
-------
Answers are cached per user for :func:`mandate_cache_seconds` (default 30s,
the same window the workspace-membership client has used for years; ``0``
disables). A cache over an authorization answer is a security defect wearing
a performance costume unless it says what invalidates it, so:

* **revocation invalidates immediately.** :func:`subscribe_mandate_invalidation`
  subscribes to the workspaces Actions that can take a mandate away
  (``workspace.member_removed``, ``workspace.member_suspended``) and drops the
  user's entry as they arrive. ``CommonDjangoConfig.ready()`` calls it, so no
  product has to remember.
* **the TTL is the bound on the bus failing**, not the normal path. If the
  event never arrives, a revoked mandate keeps opening doors for at most the
  TTL — which is why the default is 30 seconds and why a deployment that
  cannot accept any window sets it to 0.
* **grants may lag; revocations may not.** A newly granted mandate can take up
  to the TTL to be seen (a fresh membership emits nothing this module listens
  for). That direction fails toward refusal, which is the acceptable one.
* **a non-answer is never cached.** :class:`MandateLookupUnavailable` leaves
  the cache untouched, so a blip cannot be remembered as a verdict.
"""
from __future__ import annotations

import logging
from enum import Enum

from stapel_core.django.check_guard import declare_security_critical

logger = logging.getLogger(__name__)

#: The comm Function that answers the workspace-agnostic question. Named for
#: what it asks, not for a table: "does this user hold a mandate anywhere".
MANDATE_FUNCTION = "workspaces.check_mandate"

#: Payload schema of :data:`MANDATE_FUNCTION` — the contract the provider in
#: stapel-workspaces implements.
MANDATE_SCHEMA = {
    "type": "object",
    "properties": {"user_id": {"type": "string"}},
    "required": ["user_id"],
    "additionalProperties": False,
}

#: The boolean key the provider answers with.
MANDATE_RESULT_KEY = "has_mandate"

#: Actions that can take a mandate away. Subscribed to for cache invalidation.
#: A role change is deliberately absent: it moves a mandate, never removes it.
MANDATE_REVOKING_ACTIONS = (
    "workspace.member_removed",
    "workspace.member_suspended",
)

#: Seconds an answer may be reused. ``0`` disables caching entirely.
MANDATE_CACHE_SETTING = "STAPEL_MANDATE_CACHE_SECONDS"
DEFAULT_MANDATE_CACHE_SECONDS = 30

#: Timeout for the remote call. Short on purpose: a slow authorization answer
#: is a refusal that has not happened yet.
MANDATE_CALL_TIMEOUT = 3.0

#: The id IS its security-critical declaration (see ``check_guard``): there is
#: no way to reference the check without the marking travelling with it.
E001_MANDATE_SEAM_UNREACHABLE = declare_security_critical(
    "stapel_core.mandate.E001",
    "a deployment that cannot ask the mandate question refuses every mandated "
    "view; silencing the finding does not open them, it only hides why",
)


class MandateState(str, Enum):
    """The three principal states a request's principal can be in.

    A ``str`` enum so a value can be logged, compared to a literal and put on
    the wire without a conversion step — the same reason the workspaces DTO
    carries ``is_guest`` as a plain bool rather than an object.
    """

    ANONYMOUS = "anonymous"
    GUEST = "guest"
    MANDATED = "mandated"


class MandateLookupUnavailable(Exception):
    """The mandate question could not be asked — not an answer about the user.

    The membership equivalent already in this package
    (:class:`~stapel_core.django.workspaces.WorkspaceLookupUnavailable`) exists
    for the same reason and learned it the same way: a client that renders "I
    could not ask" as "you are not a member" told a workspace's own owner they
    were Forbidden for weeks. Callers turn this into 503, never 403.
    """


def mandate_cache_seconds() -> int:
    """TTL for a cached answer; ``0`` means do not cache.

    An unreadable value means the default rather than "no cache": a typo must
    not silently multiply every authorization decision by an RPC, and must not
    silently disable the invalidation path either.
    """
    from django.conf import settings

    raw = getattr(settings, MANDATE_CACHE_SETTING, DEFAULT_MANDATE_CACHE_SECONDS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MANDATE_CACHE_SECONDS
    return max(value, 0)


def _cache_key(user_id) -> str:
    return f"mandate:anywhere:{user_id}"


def invalidate_mandate_cache(user_id) -> None:
    """Forget the cached answer for *user_id*. Idempotent."""
    from django.core.cache import cache

    cache.delete(_cache_key(user_id))


def _on_mandate_revoked(event) -> None:
    """Action subscriber: a revoking event drops that user's cached answer."""
    payload = getattr(event, "payload", None) or {}
    user_id = payload.get("user_id")
    if user_id:
        invalidate_mandate_cache(user_id)


def subscribe_mandate_invalidation() -> None:
    """Wire :data:`MANDATE_REVOKING_ACTIONS` to the cache. Idempotent-safe.

    Called from ``CommonDjangoConfig.ready()``. Delivery is at-least-once and
    the handler is a cache delete, so a repeated delivery — or a repeated
    subscription — costs nothing.
    """
    from stapel_core.comm import subscribe_action

    for action in MANDATE_REVOKING_ACTIONS:
        subscribe_action(action, _on_mandate_revoked)


def _local_predicate():
    """``stapel_workspaces.permissions.has_active_mandate``, if it is here.

    Guarded exactly like ``adoption_checks.anonymous_axis_enabled`` reads
    ``stapel_auth``: core never depends on a sibling module, but it may notice
    that the deployment installed one and take the shorter path.
    """
    from django.apps import apps as django_apps

    if not django_apps.is_installed("stapel_workspaces"):
        return None
    try:
        from stapel_workspaces.permissions import has_active_mandate
    except Exception:  # pragma: no cover - installed but not importable
        return None
    return has_active_mandate


def mandate_seam_unreachable_reason() -> str | None:
    """Why this deployment cannot ask the mandate question, or None if it can.

    Settings-and-registry only, never a liveness probe — the same contract as
    ``comm.function_unreachable_reason``, whose answer this defers to. A
    process that embeds ``stapel_workspaces`` can always ask, whatever the
    comm transport is doing.
    """
    from stapel_core.comm import function_unreachable_reason

    reason = function_unreachable_reason(MANDATE_FUNCTION)
    if reason is None:
        return None
    if _local_predicate() is not None:
        return None
    return (
        f"{MANDATE_FUNCTION} is unreachable ({reason}) and stapel_workspaces "
        f"is not installed in this process, so nothing here can answer "
        f"'does this user hold a mandate anywhere'."
    )


def _ask(user) -> bool:
    """Ask the seam. Raises :class:`MandateLookupUnavailable` on any non-answer."""
    from stapel_core.comm import call, function_unreachable_reason
    from stapel_core.comm.exceptions import CommError

    user_id = str(getattr(user, "pk", None) or getattr(user, "id", None) or "")
    if not user_id:
        raise MandateLookupUnavailable(
            "the authenticated user carries no primary key to ask about"
        )

    unreachable = function_unreachable_reason(MANDATE_FUNCTION)
    if unreachable is None:
        try:
            result = call(
                MANDATE_FUNCTION, {"user_id": user_id}, timeout=MANDATE_CALL_TIMEOUT
            )
        except CommError as exc:
            logger.error(
                "mandate lookup failed for user %s over %s: %s",
                user_id, MANDATE_FUNCTION, exc,
            )
            raise MandateLookupUnavailable(str(exc)) from exc
        if not isinstance(result, dict) or MANDATE_RESULT_KEY not in result:
            logger.error(
                "mandate lookup for user %s returned no %r key: %r",
                user_id, MANDATE_RESULT_KEY, result,
            )
            raise MandateLookupUnavailable(
                f"{MANDATE_FUNCTION} answered without a {MANDATE_RESULT_KEY!r} key"
            )
        return bool(result[MANDATE_RESULT_KEY])

    predicate = _local_predicate()
    if predicate is None:
        logger.error(
            "mandate lookup impossible: %s", mandate_seam_unreachable_reason()
        )
        raise MandateLookupUnavailable(mandate_seam_unreachable_reason() or unreachable)
    try:
        return bool(predicate(user))
    except Exception as exc:  # pragma: no cover - DB down mid-request
        logger.error("in-process mandate predicate failed for user %s: %s", user_id, exc)
        raise MandateLookupUnavailable(str(exc)) from exc


def mandate_state(user) -> MandateState:
    """Which of the three states *user* is in.

    Raises:
        MandateLookupUnavailable: the question could not be asked. Never
            reported as ``GUEST`` — see the module docstring.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return MandateState.ANONYMOUS
    if getattr(user, "is_anonymous", False):
        return MandateState.ANONYMOUS

    from django.core.cache import cache

    ttl = mandate_cache_seconds()
    user_id = str(getattr(user, "pk", None) or getattr(user, "id", None) or "")
    key = _cache_key(user_id) if user_id else None

    if key and ttl:
        cached = cache.get(key)
        if cached is not None:
            return MandateState.MANDATED if cached else MandateState.GUEST

    held = _ask(user)
    if key and ttl:
        cache.set(key, bool(held), ttl)
    return MandateState.MANDATED if held else MandateState.GUEST


def has_mandate(user) -> bool:
    """True iff *user* is :attr:`MandateState.MANDATED`.

    The boolean convenience over :func:`mandate_state`; it still raises
    :class:`MandateLookupUnavailable` rather than answering False for a
    question it could not ask.
    """
    return mandate_state(user) is MandateState.MANDATED


def _views_requiring_a_mandate() -> list[str]:
    from stapel_core.django.api.permissions import HasWorkspaceMandate
    from stapel_core.django.urlsurvey import iter_surface

    seen: list[str] = []
    for entry in iter_surface():
        view = entry.view
        gates = getattr(view, "permission_classes", ()) or ()
        try:
            uses = any(
                isinstance(gate, type) and issubclass(gate, HasWorkspaceMandate)
                for gate in gates
            )
        except TypeError:  # pragma: no cover - exotic gate entries
            continue
        if not uses:
            continue
        name = f"{view.__module__}.{view.__qualname__}"
        if name not in seen:
            seen.append(name)
    return seen


def check_mandate_seam(app_configs=None, **kwargs):
    """E001 — a view requires a mandate and this deployment cannot ask about one.

    Deliberately premised on a view actually using the gate: a deployment with
    no mandated view has nothing to be wrong about, and an unconditional error
    is how a whole tag ends up in SILENCED_SYSTEM_CHECKS. Security-critical,
    because the alternative to noticing here is noticing in production, where
    every such view answers 503 — the correct behaviour, and a terrible way to
    learn the seam was never wired.

    Not on the boot-gate roster: it resolves the URLconf, and loading that
    from inside ``load_middleware()`` is a re-entrancy trap (see
    ``stapel_core.django.boot``). ``stapel_preflight`` lifts it into the
    deploy gate, which is where it belongs.
    """
    from stapel_core.django.check_guard import SecurityCriticalError

    reason = mandate_seam_unreachable_reason()
    if reason is None:
        return []
    try:
        views = _views_requiring_a_mandate()
    except Exception:  # pragma: no cover - unresolvable URLconf
        return []
    if not views:
        return []
    return [SecurityCriticalError(
        f"{len(views)} view(s) gate on HasWorkspaceMandate, but {reason} "
        f"Every request to them will be refused with 503 — fail-closed, and "
        f"invisible until the first user hits one: "
        + ", ".join(views[:5])
        + ("" if len(views) <= 5 else f", and {len(views) - 5} more"),
        hint=f"Either install stapel_workspaces in this service, or route "
             f"{MANDATE_FUNCTION!r} to the service that does — STAPEL_COMM "
             f"FUNCTION_ROUTES for the http transport, a running "
             f"`manage.py serve_functions` provider for nats. The gate is "
             f"never opened by leaving it unwired: an unanswerable "
             f"authorization question degrades to refusal.",
        id=E001_MANDATE_SEAM_UNREACHABLE,
    )]


def register_checks() -> None:
    """Register :func:`check_mandate_seam` under the ``stapel_mandate`` tag.

    A function rather than an import-time ``@checks.register`` decorator: the
    check walks the URL surface, so it must not be registered into a process
    that imported this module only for :class:`MandateState`.
    """
    from django.core import checks

    checks.register("stapel_mandate")(check_mandate_seam)


__all__ = [
    "DEFAULT_MANDATE_CACHE_SECONDS",
    "E001_MANDATE_SEAM_UNREACHABLE",
    "MANDATE_CACHE_SETTING",
    "MANDATE_CALL_TIMEOUT",
    "MANDATE_FUNCTION",
    "MANDATE_RESULT_KEY",
    "MANDATE_REVOKING_ACTIONS",
    "MANDATE_SCHEMA",
    "MandateLookupUnavailable",
    "MandateState",
    "check_mandate_seam",
    "has_mandate",
    "invalidate_mandate_cache",
    "mandate_cache_seconds",
    "mandate_seam_unreachable_reason",
    "mandate_state",
    "register_checks",
    "subscribe_mandate_invalidation",
]
