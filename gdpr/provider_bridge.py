"""A registered ``GDPRProvider`` answers an erasure by being registered.

THE FAILURE
-----------
A classified fleet's ``svc-classified-core`` installs stapel-listings,
stapel-reviews and stapel-moderation. All three register a
:class:`~stapel_core.gdpr.GDPRProvider` in ``gdpr_registry`` from their
``AppConfig.ready()``. The host declares all three in
``STAPEL_GDPR["DATA_OWNERS"]``. Every visible part of it looks wired.

None of them answered an erasure, and none of them ever could.

``gdpr_registry`` is the MONOLITH path: stapel-gdpr's orchestrator walks
``gdpr_registry.providers`` in its own process. In a split fleet the provider
sits in a peer, and the only thing that reaches a peer is
``gdpr.erasure.requested`` — which requires a SECOND, separate act of
registration, :func:`~stapel_core.gdpr.owners.register_gdpr_owner`, that those
libraries never perform. stapel-agent does perform it from the same
``ready()`` that registers its provider, which is exactly why ``agent``
answered on that fleet and ``listings``, ``reviews`` and ``moderation`` did
not.

Measured live on 2026-09-17, on erasure request **id 1** — the first ever run
on a fleet with real users. Ten declared owners, six that could answer, four
``ErasurePart`` rows stuck ``pending`` with ``completeness_waived=False``, so
the request can never reach ``deleted``. Nobody had found out, because finding
out requires running an erasure.

WHY A BRIDGE AND NOT A LINE IN THE DOCS
---------------------------------------
Requiring every library to remember a second registration makes correctness
something each consumer opts into, and this failure is silent by construction:
every part reports success except the ones that report nothing at all, and an
orchestrator waiting on silence looks identical to one waiting on slow work.

A provider is already a declaration of ownership — it names its ``section``
and implements ``delete``/``anonymize``. Being registered is the act. So core
answers on its behalf, and a library that wants to answer for itself still
can.

WHY AT DISPATCH AND NOT AT ``ready()``
--------------------------------------
``AppConfig.ready()`` runs in ``INSTALLED_APPS`` order. A bridge that
registered eagerly would race any library that wires itself explicitly — and
:func:`register_gdpr_owner` raises ``one name is one owner`` on a second
registration with different terms, so the winner, and whether the process
booted at all, would depend on a list's order. At dispatch time every
``ready()`` has run and "does this section already have an explicit owner?"
has a settled answer.

The bridge builds the very handlers :func:`register_gdpr_owner` would have
built (:func:`~stapel_core.gdpr.owners._build`) and hands them the event, so
there is no second copy of the protocol: the transaction that makes a rolled
back erasure produce no receipt, the deterministic receipt id, the silence for
an unclaimed subject type, the logged drop for a malformed payload and the
probe answered from the same module are all the ones already tested here.
"""
from __future__ import annotations

import logging
import threading

from stapel_core.gdpr import gdpr_registry

logger = logging.getLogger(__name__)

E011 = "stapel_core.gdpr.E011"

#: A provider's interface is ``delete(user_id)`` / ``anonymize(user_id)``. It
#: is keyed by a user and nothing else, so the bridge claims ``account`` and
#: only ``account`` — answering the probe with a type it cannot erase would
#: make the liveness answer a lie, which is the defect one level up.
BRIDGED_SUBJECT_TYPES = ("account",)

_built: dict[str, object] = {}
_lock = threading.Lock()
_subscribed = False


def _erase_for(provider):
    """``erase(subject_type, subject_key, workspace_id)`` over a provider.

    Runs the provider the way the monolith orchestrator runs it —
    ``anonymize`` and then ``delete`` — so a section behaves identically
    whether it sits in the orchestrator's process or a peer's. That order is
    not cosmetic: ``anonymize`` is what keeps retained content (reviews, chat)
    readable after the identity behind it is gone, and running it after a hard
    delete would have nothing left to rewrite.

    Returns ``{}`` and not ``None``: ``None`` means "this key names nothing of
    mine" and receipts nothing. The provider protocol reports no counts, so
    the honest answer is a receipt with an empty tally — the work happened,
    and this interface cannot say how much.
    """

    def erase(subject_type, subject_key, workspace_id=None):
        if subject_type not in BRIDGED_SUBJECT_TYPES:
            return None
        provider.anonymize(subject_key)
        provider.delete(subject_key)
        return {}

    return erase


def _bridged_owner(provider):
    """The handlers :func:`register_gdpr_owner` would have built, uncached."""
    from stapel_core.gdpr.owners import _build

    section = str(provider.section)
    with _lock:
        built = _built.get(section)
        if built is None or getattr(built, "erase", None) is None:
            built = _build(
                section,
                BRIDGED_SUBJECT_TYPES,
                _erase_for(provider),
                False,  # legacy user.deleted stays the explicit wiring's job
                getattr(provider.__class__, "__module__", "") or "",
            )
            _built[section] = built
    return built


def _unbridged_providers():
    """Registered providers whose section has no owner of its own.

    Asked fresh on every event: a library that wires itself explicitly is
    authoritative, whenever in the boot it got round to saying so.
    """
    from stapel_core.gdpr.owners import registered_gdpr_owners

    explicit = set(registered_gdpr_owners())
    return [p for p in gdpr_registry.providers if str(p.section) not in explicit]


def bridge_erasure_requested(event) -> None:
    """Answer ``gdpr.erasure.requested`` for every unbridged provider."""
    for provider in _unbridged_providers():
        _bridged_owner(provider).handle_erasure_requested(event)


def bridge_owner_probe(event) -> None:
    """Answer ``gdpr.owner.probe`` for every unbridged provider.

    From the same module that erases, so ``gdpr.owner.alive`` stays evidence
    that the erasure path is consumed rather than that a container is running.
    """
    for provider in _unbridged_providers():
        _bridged_owner(provider).handle_owner_probe(event)


def register_provider_bridge() -> bool:
    """Subscribe the bridge. Called from core's ``AppConfig.ready()``.

    One subscription for the whole process, not one per section: the set of
    sections is read at dispatch, so a provider registered by a later app is
    covered without a second subscribe.
    """
    global _subscribed
    from stapel_core.comm import subscribe_action
    from stapel_core.gdpr.owners import (
        ERASURE_REQUESTED,
        ERASURE_REQUESTED_SCHEMA,
        OWNER_PROBE,
        OWNER_PROBE_SCHEMA,
    )

    if _subscribed:
        return False
    subscribe_action(
        ERASURE_REQUESTED, bridge_erasure_requested, schema=ERASURE_REQUESTED_SCHEMA
    )
    subscribe_action(OWNER_PROBE, bridge_owner_probe, schema=OWNER_PROBE_SCHEMA)
    _subscribed = True
    return True


def _bridge_is_subscribed() -> bool:
    return _subscribed


def _reset_bridge() -> None:
    """Tests only — forget the built handlers and the subscribe flag."""
    global _subscribed
    with _lock:
        _built.clear()
    _subscribed = False


def check_owners_are_answerable(app_configs=None, **kwargs):
    """System check: a section this process owns can be reached over comm.

    The gap this closes was found by running an erasure, which is the worst
    way to find it — four owners had been unable to answer since the fleet was
    built, and the first person to learn that would have been a subject
    exercising their right to erasure. A declared owner with no reachable
    answerer is a boot-time fact, so it belongs at boot.
    """
    from django.core.checks import Error

    if _bridge_is_subscribed():
        return []
    stranded = sorted(str(p.section) for p in _unbridged_providers())
    if not stranded:
        return []
    return [
        Error(
            f"GDPR provider section(s) {', '.join(stranded)} are registered in "
            f"gdpr_registry but nothing in this process answers "
            f"gdpr.erasure.requested for them. gdpr_registry is the monolith "
            f"path — the orchestrator walks it in ITS OWN process — so in a "
            f"split fleet these sections are declared owners that can never "
            f"receipt, and every erasure naming them waits forever while every "
            f"other part reports success. Either let core's provider bridge "
            f"subscribe (stapel_core.gdpr.provider_bridge."
            f"register_provider_bridge, called from core's AppConfig.ready), "
            f"or register each section explicitly with "
            f"stapel_core.gdpr.register_gdpr_owner.",
            id=E011,
            obj=", ".join(stranded),
        )
    ]


__all__ = [
    "E011",
    "BRIDGED_SUBJECT_TYPES",
    "bridge_erasure_requested",
    "bridge_owner_probe",
    "check_owners_are_answerable",
    "register_provider_bridge",
]
