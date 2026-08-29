"""Lifecycle-pair checks (tag ``stapel_lifecycle``) — a merge is not a delete.

An adoption check in the sense of ``django/adoption_checks.py``: derivable
premise, derivable obligation, and an explicit declaration instead of silence.
The premise here is not a setting but a *subscription* — the app told the
action registry it cares about an account's life cycle, so the rest of that
life cycle is its business too.

Why the pair exists
-------------------
``user.deleted`` says a person's rows are to go away. ``user.merged``
(stapel-auth 0.30.0) says the opposite: a guest account was folded into a
survivor on sign-in and its rows are to be **re-parented**, not erased. The
two events reach the same tables through the same registry, and an app that
subscribed to one and not the other is not neutral about the other — it has a
silent, wrong answer for it. A merge re-parents rows; an app that only knows
deletion strands them: the guest's wallet, profile, prompt log or listing
stays pointed at a user id that no longer signs in, invisible to the survivor
and never erased, because no erasure was ever requested for it.

That failure has no symptom at the seam. Nothing raises, nothing retries,
nothing is logged — the rows are simply orphaned, and the first report is a
person saying the thing they made as a guest is gone. It is exactly the shape
of defect that CI cannot see, because each library's own tests pass: the
handler that is missing is the one nobody wrote a test for.

Silence is the finding, not the policy
--------------------------------------
The check never demands a particular merge policy, and cannot: summing two
wallets, keeping the survivor's profile, or doing nothing at all are all
legitimate answers, and which one is right is the library's call. So an
explicit no-op handler is a green answer::

    @on_action("user.merged")
    def handle_user_merged(event):
        \"\"\"No per-user rows here — nothing to re-parent.\"\"\"

That line costs nothing and is worth the check on its own: after it, "this
module holds nothing that survives a merge" is a fact someone wrote down,
instead of an absence a reader has to prove.

Attribution
-----------
A handler is attributed to the app whose ``AppConfig.name`` is the longest
prefix of the handler's defining module; a handler from a module no installed
app claims is reported under its top-level package. Handlers registered on a
library's behalf by core — the ``user.deleted`` subscriber that
:func:`stapel_core.gdpr.register_gdpr_owner` builds as a closure — carry a
``stapel_handler_module`` stamp naming the library that asked for them, so
they are charged to that library and not to ``stapel_core``.

Checks
------
E001  an app handles a lifecycle event whose companion event has no handler
      anywhere in that app.
"""
from __future__ import annotations

import inspect

from django.apps import apps
from django.core import checks

E001_LIFECYCLE_PAIR_UNHANDLED = "stapel_core.lifecycle.E001"

#: cause action -> the companion action a subscriber of the cause must also
#: answer. One entry today; the table is the extension point (a future
#: ``user.anonymized`` pairs the same way).
LIFECYCLE_PAIRS: dict[str, str] = {
    "user.deleted": "user.merged",
}

_WHY = {
    "user.merged": (
        "a merge re-parents rows to the surviving account; an app that only "
        "knows deletion strands them — the merged user's rows keep pointing "
        "at an id that can no longer sign in, and no erasure is ever "
        "requested for it."
    ),
}


def _handler_module(handler) -> str:
    """The module a handler should be charged to.

    ``stapel_handler_module`` wins: it is set where core subscribes a handler
    on another package's behalf, and the closure's own ``__module__`` would
    name core instead of the library that asked.
    """
    stamped = getattr(handler, "stapel_handler_module", None)
    if stamped:
        return str(stamped)
    try:
        handler = inspect.unwrap(handler)
    except Exception:  # noqa: BLE001 — a broken __wrapped__ chain is not our error
        pass
    return str(getattr(handler, "__module__", "") or "")


def _owner_of(module: str) -> str:
    """Longest installed ``AppConfig.name`` that owns *module*.

    Falls back to the top-level package so a handler living outside every
    installed app is still named in the report rather than dropped.
    """
    best = ""
    for config in apps.get_app_configs():
        name = config.name
        if module == name or module.startswith(f"{name}."):
            if len(name) > len(best):
                best = name
    return best or module.split(".")[0]


def _owners_subscribed_to(action: str) -> set[str]:
    from .registry import action_registry

    owners = set()
    for handler in action_registry.handlers(action):
        module = _handler_module(handler)
        if module:
            owners.add(_owner_of(module))
    return owners


@checks.register("stapel_lifecycle")
def check_lifecycle_pairs(app_configs=None, **kwargs):
    """E001 — an app answers one half of an account life cycle and not the other."""
    selected = None
    if app_configs is not None:
        selected = {config.name for config in app_configs}

    errors = []
    for cause, companion in sorted(LIFECYCLE_PAIRS.items()):
        unpaired = _owners_subscribed_to(cause) - _owners_subscribed_to(companion)
        for owner in sorted(unpaired):
            if selected is not None and owner not in selected:
                continue
            errors.append(
                checks.Error(
                    f"app {owner!r} handles the {cause!r} action but registers "
                    f"no handler for {companion!r} — "
                    + _WHY.get(companion, "the companion event goes unanswered."),
                    hint=(
                        f"Subscribe {companion!r} in the same module: re-parent "
                        f"this app's per-user rows to the surviving account "
                        f"(idempotently — delivery is at-least-once), or, if "
                        f"this app holds no rows that survive the event, "
                        f"register an explicit no-op handler saying so. Both "
                        f"answers are green; only silence is reported."
                    ),
                    id=E001_LIFECYCLE_PAIR_UNHANDLED,
                )
            )
    return errors


__all__ = [
    "E001_LIFECYCLE_PAIR_UNHANDLED",
    "LIFECYCLE_PAIRS",
    "check_lifecycle_pairs",
]
