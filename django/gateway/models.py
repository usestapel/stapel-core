"""Storage of the privilege gateway: scope tokens and pending actions.

``ScopeToken`` — server-side table of opaque project-scoped tokens (only
sha256 hashes are stored; the plaintext exists once, in the issuer's
response). Revocation is a row update — instant, no signature blacklist.

``PendingAction`` — parked ``require_confirmation`` calls: the validated
verb + args + caller identity, waiting for an out-of-band human decision.
The confirming side is never the container (a hijacked agent must not
confirm its own destructive action) — confirmation flows through the comm
Function / Python API in the control plane only.
"""
import uuid

from django.db import models


class ScopeToken(models.Model):
    id = models.BigAutoField(primary_key=True)
    token_hash = models.CharField(max_length=64, unique=True)
    project = models.CharField(max_length=255, db_index=True)
    container = models.CharField(max_length=255, null=True, blank=True)
    # Network identity binding: exact IP or CIDR the token may speak from.
    network = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"scope-token#{self.id} {self.project}"


class PendingAction(models.Model):
    STATUS_PENDING = "pending"
    STATUS_EXECUTED = "executed"
    STATUS_FAILED = "failed"
    STATUS_REJECTED = "rejected"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_PENDING, "pending"),
        (STATUS_EXECUTED, "executed"),
        (STATUS_FAILED, "failed"),
        (STATUS_REJECTED, "rejected"),
        (STATUS_EXPIRED, "expired"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    verb = models.CharField(max_length=255, db_index=True)
    args = models.JSONField(default=dict, blank=True)
    # Frozen caller identity from the original request.
    channel = models.CharField(max_length=32)
    project = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    container = models.CharField(max_length=255, null=True, blank=True)
    tier = models.CharField(max_length=64, null=True, blank=True)
    subject = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=255, null=True, blank=True)

    def __str__(self):
        return f"pending-action {self.id} {self.verb} [{self.status}]"
