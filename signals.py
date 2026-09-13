"""Django signals for business milestones.

In-process extension points complementing the comm layer: a host project
hooks local behavior (analytics, cache warm-up, denormalization) without
forking and without a broker round-trip. Cross-module/cross-service
communication still goes through comm Actions — signals are same-process
only and carry no delivery guarantees.

    from stapel_core.signals import user_registered

    @receiver(user_registered)
    def on_registered(sender, user, **kwargs): ...

Senders pass documented kwargs; providing_args are listed per signal.
"""
import django.dispatch

# stapel_auth: user completed registration (kwargs: user, request=None)
user_registered = django.dispatch.Signal()

# stapel_auth: user authenticated a new session (kwargs: user, request=None)
user_logged_in = django.dispatch.Signal()

# stapel_billing: money accepted (kwargs: user, credits, transaction)
payment_completed = django.dispatch.Signal()

# stapel_billing: plan/status changed (kwargs: subscription)
subscription_changed = django.dispatch.Signal()

# stapel_cdn: variants generated (kwargs: instance)
media_processed = django.dispatch.Signal()

# stapel_profiles: profile mutated (kwargs: profile, fields_changed=None)
profile_updated = django.dispatch.Signal()

# stapel_workspaces: membership created/role changed/removed
# (kwargs: workspace, user, role, action: "added"|"updated"|"removed")
workspace_member_changed = django.dispatch.Signal()

# stapel_core.bus.dlq: an event or a task was given up on and parked
# (kwargs: topic, event, reason, exc_info). The counter
# ``bus_dlq_total`` says HOW MUCH work is being dropped; a deployment that
# wants to know WHAT was dropped — an alert store, an on-call bot — has had
# to scrape log lines for it. This is the same fact, in process, with the
# traceback still attached: ``exc_info`` is ``sys.exc_info()`` when the park
# happened inside an exception handler (every handler failure), else None.
#
# Receivers must not raise: the sender is already on a failure path and
# swallows everything, so an exception here is silently lost, not surfaced.
bus_event_parked = django.dispatch.Signal()

__all__ = [
    "user_registered",
    "user_logged_in",
    "payment_completed",
    "subscription_changed",
    "media_processed",
    "profile_updated",
    "workspace_member_changed",
    "bus_event_parked",
]
