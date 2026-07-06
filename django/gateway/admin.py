"""Admins for the privilege gateway (admin-suite AS-3 §1.3).

``ScopeToken`` is a ``secret`` model — superuser-only, and even there the
stored hash never reaches the response (masked; only sha256 hashes are stored
to begin with, but a hash is still an offline-verification oracle).
``PendingAction`` is an ops journal: confirmation flows through the comm
Functions in the control plane, never through the admin.
"""
from django.contrib import admin

from stapel_core.django.admin.base import StapelModelAdmin

from .models import PendingAction, ScopeToken


@admin.register(ScopeToken)
class ScopeTokenAdmin(StapelModelAdmin):
    list_display = ("id", "project", "container", "network", "created_at", "expires_at", "revoked_at")
    search_fields = ("project", "container")
    ordering = ("-created_at",)
    secret_fields = ("token_hash",)  # explicit — pattern detection would match too


@admin.register(PendingAction)
class PendingActionAdmin(StapelModelAdmin):
    list_display = ("id", "verb", "status", "channel", "project", "created_at", "expires_at", "resolved_by")
    list_filter = ("status", "channel")
    search_fields = ("verb", "project", "container", "subject", "resolved_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
