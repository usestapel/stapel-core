"""Signals of the access package."""
import django.dispatch

#: Sent by :class:`~stapel_core.access.backend.AuditedModelBackend` every
#: time a manual DAC grant is used *above* a staff user's mandate (A4 —
#: escalation is allowed by default, but never silent). kwargs:
#: ``user``, ``perm`` (full "app_label.codename"), ``required`` (Level),
#: ``clearance`` (Level). Subscribe to forward into your audit pipeline
#: (eventstore, SIEM, notifications).
dac_escalation = django.dispatch.Signal()

#: Sent by :class:`~stapel_core.django.admin.base.StapelModelAdmin` when a
#: step-up-gated admin operation (its required level is in
#: ``STAPEL_ACCESS["STEP_UP"]["LEVELS"]``) is refused because the user holds
#: no fresh verification grant for the step-up scope (AS-6, Q8a). kwargs:
#: ``user``, ``label`` (``"app_label.ModelName"``), ``action``
#: (view/add/change/delete), ``scope``. Subscribe to forward into your audit
#: pipeline — the built-in :mod:`stapel_core.access.audit` receiver already
#: does, emitting ``access.step_up_denied``.
step_up_denied = django.dispatch.Signal()

__all__ = ["dac_escalation", "step_up_denied"]
