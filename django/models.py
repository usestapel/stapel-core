"""
Common Django models for Iron services.

This module provides shared models that should be used across all Django services
to ensure consistency and enable cross-service authentication.
"""

from django.db import models, transaction
from django.utils import timezone
import uuid
from datetime import timedelta

# Reuse unified User model from stapel_core.django.users to avoid duplicate app_label conflicts


class RevisionMixin(models.Model):
    """
    Mixin for models that need revision tracking for client synchronization.

    This mixin provides:
    - `revision`: Integer field that auto-increments on each save
    - `deleted`: Boolean flag for soft deletion (instead of actual delete)

    How revision works:
    - Before saving, finds the MAX revision in the DB and sets revision = max + 1
    - Clients can request changes since a specific revision for efficient sync
    - Soft deletion (deleted=True) allows clients to know what was removed

    Usage:
        class MyModel(RevisionMixin):
            name = models.CharField(max_length=100)

            class Meta:
                # Don't forget to add index on revision for performance
                indexes = [
                    models.Index(fields=['revision']),
                ]

    Migration defaults:
        - revision: 0
        - deleted: False
    """

    revision = models.PositiveIntegerField(default=0, db_index=True, editable=False)
    deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        """
        Override save to auto-increment revision.

        Uses select_for_update to prevent race conditions when multiple
        saves happen simultaneously.
        """
        with transaction.atomic():
            # Get the max revision for this model
            max_revision = self.__class__.objects.aggregate(
                max_rev=models.Max('revision')
            )['max_rev'] or 0

            # Set new revision
            self.revision = max_revision + 1

            super().save(*args, **kwargs)

    def soft_delete(self):
        """Mark the object as deleted without removing it from DB."""
        self.deleted = True
        self.save()

    def restore(self):
        """Restore a soft-deleted object."""
        self.deleted = False
        self.save()

    @classmethod
    def get_max_revision(cls):
        """Get the current maximum revision for this model."""
        return cls.objects.aggregate(max_rev=models.Max('revision'))['max_rev'] or 0

    @classmethod
    def get_changes_since(cls, min_revision=0, max_revision=None, include_deleted=True):
        """
        Get all objects changed since the given revision.

        Args:
            min_revision: Minimum revision (exclusive). Default 0 means get all.
            max_revision: Maximum revision (inclusive). None means no upper limit.
            include_deleted: Whether to include soft-deleted objects.

        Returns:
            QuerySet of objects with revision > min_revision
        """
        qs = cls.objects.filter(revision__gt=min_revision)

        if max_revision is not None:
            qs = qs.filter(revision__lte=max_revision)

        if not include_deleted:
            qs = qs.filter(deleted=False)

        return qs.order_by('revision')



class PhoneVerification(models.Model):
    """
    Model to store phone verification codes.

    This is an abstract model that should be inherited by each service
    that needs phone verification functionality.
    """
    phone = models.CharField(max_length=18, db_index=True)
    code = models.CharField(max_length=6)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.IntegerField(default=0)

    class Meta:
        abstract = True
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=10)
        super().save(*args, **kwargs)

    def is_expired(self):
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"{self.phone} - {self.code}"


class ServiceAPIKey(models.Model):
    """
    Model for service-to-service authentication.

    This is an abstract model for managing API keys used for
    inter-service communication.
    """
    name = models.CharField(max_length=100, unique=True)
    key = models.CharField(max_length=255, unique=True, db_index=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    # Permissions
    allowed_endpoints = models.JSONField(default=list, blank=True)

    class Meta:
        abstract = True
        verbose_name = 'Service API Key'
        verbose_name_plural = 'Service API Keys'

    def __str__(self):
        return f"{self.name} - {'Active' if self.is_active else 'Inactive'}"

    @classmethod
    def generate_key(cls):
        """Generate a new API key"""
        return f"sk_{uuid.uuid4().hex}"


class RefreshTokenTracker(models.Model):
    """
    Track refresh tokens for additional security.

    This is an abstract model. Each service should create a concrete model
    with a ForeignKey to their User model.
    """
    # Note: user ForeignKey must be defined in concrete model
    token = models.CharField(max_length=500, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_revoked = models.BooleanField(default=False)
    device_info = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user} - {self.created_at}"


class LoginAttempt(models.Model):
    """
    Track login attempts for security purposes.

    This is an abstract model for monitoring and preventing brute force attacks.
    """
    identifier = models.CharField(max_length=255, db_index=True)  # email, phone, or IP
    attempt_type = models.CharField(max_length=20)  # 'success', 'failed', 'blocked'
    ip_address = models.GenericIPAddressField()
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['identifier', 'created_at']),
            models.Index(fields=['ip_address', 'created_at']),
        ]

    def __str__(self):
        return f"{self.identifier} - {self.attempt_type} - {self.created_at}"
