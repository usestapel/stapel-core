"""Access audit forwarding — dac_escalation / step_up_denied → eventstore.

Two access signals carry the security-relevant admin events:

- :data:`~stapel_core.access.signals.dac_escalation` — a manual DAC grant was
  honored *above* a staff user's mandate (A4, emitted by AuditedModelBackend).
- :data:`~stapel_core.access.signals.step_up_denied` — a HIGH-class admin
  operation was refused for lack of a fresh verification grant (AS-6).

:func:`connect_access_audit` (called from ``CommonDjangoConfig.ready()``)
subscribes a receiver that forwards each as one event —
``access.dac_escalation`` / ``access.step_up_denied`` — to the audit sink
(``STAPEL_ACCESS["AUDIT_SINK"]``, default the core eventstore, stream
``STAPEL_ACCESS["AUDIT_STREAM"]``), then to the optional ``NOTIFY`` alerting
shim. The sink signature mirrors the gateway's:
``callable(stream, payload, *, project, container)``.

**Best-effort, unlike the gateway.** The gateway's audit *is* the
authorization record on a privileged mutation path, so it fails closed. Here
the durable record already exists — AuditedModelBackend logs every escalation,
StapelModelAdmin logs every denial — and ``dac_escalation`` fires *inside*
``has_perm``; a raising receiver would break permission checks and lock admins
out on a mere telemetry outage. So a sink/notify failure is logged and
swallowed, never raised.
"""
from __future__ import annotations

import logging
from typing import Any

from .signals import dac_escalation, step_up_denied

logger = logging.getLogger("stapel_core.access")

#: dispatch_uids keep :func:`connect_access_audit` idempotent across repeated
#: ``ready()`` runs (e.g. override_settings(INSTALLED_APPS=...) in tests).
_DAC_UID = "stapel_access.audit.dac_escalation"
_STEP_UP_UID = "stapel_access.audit.step_up_denied"


def eventstore_sink(stream: str, payload: dict, *, project: str | None = None, container: str | None = None) -> None:
    """Default sink: append the audit line to the core event store."""
    from stapel_core import eventstore

    eventstore.append(stream, payload, project=project, container=container)


def _forward(event: str, payload: dict[str, Any]) -> None:
    """Send one access audit line to the sink, then the NOTIFY shim."""
    from .conf import access_settings

    body = {"event": event, **payload}
    try:
        sink = access_settings.AUDIT_SINK
        sink(access_settings.AUDIT_STREAM, body, project=None, container=None)
    except Exception:
        logger.exception("access audit sink failed for %s", event)

    notify = access_settings.NOTIFY
    if notify is not None:
        try:
            notify(event, body)
        except Exception:
            logger.exception("access NOTIFY hook failed for %s", event)


def _on_dac_escalation(sender, user=None, perm=None, clearance=None, required=None, **kwargs):
    _forward("access.dac_escalation", {
        "user_id": str(getattr(user, "pk", None)),
        "perm": perm,
        "clearance": clearance.name.lower() if clearance is not None else None,
        "required": required.name.lower() if required is not None else None,
    })


def _on_step_up_denied(sender, user=None, label=None, action=None, scope=None, **kwargs):
    _forward("access.step_up_denied", {
        "user_id": str(getattr(user, "pk", None)),
        "model": label,
        "action": action,
        "scope": scope,
    })


def connect_access_audit() -> None:
    """Subscribe the audit receivers (idempotent via dispatch_uid)."""
    dac_escalation.connect(_on_dac_escalation, dispatch_uid=_DAC_UID)
    step_up_denied.connect(_on_step_up_denied, dispatch_uid=_STEP_UP_UID)


__all__ = ["connect_access_audit", "eventstore_sink"]
