"""
Common Django models for Stapel services.

This module provides shared models that should be used across all Django services
to ensure consistency and enable cross-service authentication.
"""

import threading
import uuid
import zlib
from datetime import timedelta

from django.db import models, router, transaction
from django.utils import timezone

# Reuse unified User model from stapel_core.django.users to avoid duplicate app_label conflicts


# Process-local mutexes serializing revision issuance per (db alias, table).
# Used on backends without PostgreSQL advisory locks (e.g. the SQLite minimal
# profile, where SELECT ... FOR UPDATE is unavailable and two concurrent
# transactions would otherwise read the same MAX(revision)).
_revision_locks: dict = {}
_revision_locks_guard = threading.Lock()


def _get_revision_lock(alias: str, table: str) -> threading.Lock:
    key = (alias, table)
    with _revision_locks_guard:
        lock = _revision_locks.get(key)
        if lock is None:
            lock = _revision_locks[key] = threading.Lock()
        return lock


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

    ``save(update_fields=...)`` contract (sync semantics):
    - ``save()`` without ``update_fields`` bumps the revision — the write is a
      content change that sync clients must see.
    - ``save(update_fields=[...])`` WITHOUT ``"revision"`` in the set does NOT
      bump: the caller explicitly scoped the write to non-synced fields
      (drafts, counters, bookkeeping). DB row, in-memory instance and
      post_save receivers all keep the current revision — no phantom numbers.
    - ``save(update_fields=[..., "revision"])`` is the explicit opt-in: the
      revision is bumped and persisted together with the listed fields.

    Concurrency: revision issuance is serialized so two concurrent saves can
    never share a number (a duplicate would make ``get_changes_since`` skip
    one of them forever):
    - PostgreSQL: a transaction-scoped advisory lock keyed on the table name
      (``pg_advisory_xact_lock``) is held until COMMIT, so numbers are also
      commit-ordered across processes.
    - Other backends (SQLite minimal profile, ...): a process-local mutex per
      (db alias, table) serializes issue+commit. This is safe for
      single-process deployments; when the save is nested in an outer
      ``transaction.atomic`` the mutex is released before the outer COMMIT,
      so multi-threaded writers with long outer transactions should prefer
      PostgreSQL (or SQLite ``"transaction_mode": "IMMEDIATE"``).

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

    def _lock_revision_scope(self, using: str) -> None:
        """Serialize revision issuance for this model's table (PostgreSQL).

        ``pg_advisory_xact_lock`` is released at transaction end, so a
        concurrent writer blocks until this save's outermost COMMIT and then
        reads the committed MAX — numbers are unique AND commit-ordered
        (a sync client that saw revision N can never later miss N-1).
        Key collisions between tables only over-serialize, never corrupt.
        """
        connection = transaction.get_connection(using)
        if connection.vendor == 'postgresql':
            key = zlib.crc32(self._meta.db_table.encode())
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s)', [key])

    def save(self, *args, **kwargs):
        """
        Override save to auto-increment revision on content changes.

        See the class docstring for the ``update_fields`` contract and the
        concurrency guarantees (advisory lock on PostgreSQL, process-local
        mutex elsewhere).
        """
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            # Normalize once: membership check below must not consume a
            # one-shot iterable before Django sees it.
            update_fields = kwargs['update_fields'] = frozenset(update_fields)
        if update_fields is not None and 'revision' not in update_fields:
            # Caller scoped the write to non-synced fields: not a content
            # change. Bumping here would either be lost (revision not in the
            # persisted set — the H-3 phantom) or silently widen the write.
            super().save(*args, **kwargs)
            return

        using = kwargs.get('using') or router.db_for_write(
            self.__class__, instance=self
        )
        connection = transaction.get_connection(using)

        def _issue_and_save():
            with transaction.atomic(using=using):
                self._lock_revision_scope(using)

                # Get the max revision for this model
                max_revision = self.__class__.objects.using(using).aggregate(
                    max_rev=models.Max('revision')
                )['max_rev'] or 0

                # Set new revision
                self.revision = max_revision + 1

                super(RevisionMixin, self).save(*args, **kwargs)

        if connection.vendor == 'postgresql':
            _issue_and_save()
        else:
            # No advisory locks: hold a process-local mutex across issue AND
            # commit (the atomic block) so a concurrent thread cannot read a
            # stale MAX before this number is durable.
            with _get_revision_lock(using, self._meta.db_table):
                _issue_and_save()

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
