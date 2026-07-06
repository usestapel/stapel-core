"""Signals of the access package."""
import django.dispatch

#: Sent by :class:`~stapel_core.access.backend.AuditedModelBackend` every
#: time a manual DAC grant is used *above* a staff user's mandate (A4 —
#: escalation is allowed by default, but never silent). kwargs:
#: ``user``, ``perm`` (full "app_label.codename"), ``required`` (Level),
#: ``clearance`` (Level). Subscribe to forward into your audit pipeline
#: (eventstore, SIEM, notifications).
dac_escalation = django.dispatch.Signal()

__all__ = ["dac_escalation"]
