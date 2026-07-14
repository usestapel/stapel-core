import uuid
from datetime import timedelta
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils import timezone


class AbstractStapelUser(AbstractUser):
    """All Stapel user fields and behavior, as an abstract base.

    Host projects that need extra fields subclass this and point
    AUTH_USER_MODEL at their model — no fork required. Feature modules must
    reference the user via settings.AUTH_USER_MODEL / get_user_model(),
    never the concrete class below.
    """

    AUTH_TYPE_CHOICES = [
        ("email", "Email"),
        ("phone", "Phone"),
        ("oauth", "OAuth"),
        ("anonymous", "Anonymous"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Note: unique constraints removed from model - auth service validates uniqueness in business logic
    # This allows other services to sync users from JWT without constraint violations
    email = models.EmailField(_("email address"), null=True, blank=True)
    phone = models.CharField(max_length=18, null=True, blank=True)
    auth_type = models.CharField(max_length=20, choices=AUTH_TYPE_CHOICES, default="email")
    is_email_verified = models.BooleanField(default=False)
    is_phone_verified = models.BooleanField(default=False)
    is_anonymous = models.BooleanField(default=False)
    anonymous_created_at = models.DateTimeField(null=True, blank=True)

    # Staff roles (admin-suite AS-2): materialized role names for the JWT
    # claim. Auth service is the single writer (A2); shadow copies in
    # consumer services are REPLACED from the staff_roles claim (в.3).
    staff_roles = models.JSONField(default=list, blank=True)

    # User status fields
    onboarding_completed = models.BooleanField(default=False)
    profile_completed = models.BooleanField(default=False)

    # OAuth fields
    oauth_provider = models.CharField(max_length=50, null=True, blank=True)
    oauth_id = models.CharField(max_length=255, null=True, blank=True)

    # Profile fields
    # URLField defaults to max_length=200 — too short for OAuth provider avatar
    # URLs (Google/GitHub picture URLs routinely exceed 200 chars with sizing
    # params), which caused StringDataRightTruncation on OAuth signup. Widened;
    # the OAuth ingestion point also drops an over-long avatar rather than crash.
    avatar = models.URLField(max_length=500, null=True, blank=True)
    bio = models.TextField(max_length=500, blank=True)

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)

    # Override M2M fields to use stable related names under app_label "users"
    groups = models.ManyToManyField(
        "auth.Group",
        related_name="%(app_label)s_%(class)s_set",
        related_query_name="%(app_label)s_%(class)s",
        blank=True,
        help_text=_("The groups this user belongs to."),
        verbose_name=_("groups"),
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission",
        related_name="%(app_label)s_%(class)s_permissions_set",
        related_query_name="%(app_label)s_%(class)s_permissions",
        blank=True,
        help_text=_("Specific permissions for this user."),
        verbose_name=_("user permissions"),
    )

    # USERNAME_FIELD must be unique - use username since email/phone may not be set
    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = []

    class Meta(AbstractUser.Meta):
        abstract = True

    def save(self, *args, **kwargs):
        # Normalize empty strings to NULL for unique constraints
        # PostgreSQL allows multiple NULLs but not multiple empty strings
        if self.email == '':
            self.email = None
        if self.phone == '':
            self.phone = None
        elif self.phone:
            # Normalize phone to E.164 format (+79991234567)
            try:
                import phonenumbers
                parsed = phonenumbers.parse(self.phone, None)
                if phonenumbers.is_valid_number(parsed):
                    self.phone = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            except Exception:
                pass  # Keep as-is if parsing fails

        # NULL and '' both pass has_usable_password() because Django only checks
        # for the '!' prefix. Normalise both to the proper unusable marker.
        if self.password in ('', None):
            from django.contrib.auth.hashers import make_password
            self.password = make_password(None)

        if not self.username:
            self.username = f"user_{uuid.uuid4().hex[:8]}"
        super().save(*args, **kwargs)

    def __str__(self):
        if self.is_anonymous:
            return f"Anonymous User {self.id}"
        return self.email or self.phone or self.username

    @classmethod
    def create_anonymous_user(cls):
        user = cls.objects.create(
            username=f"anon_{uuid.uuid4().hex[:8]}",
            auth_type="anonymous",
            is_anonymous=True,
            anonymous_created_at=timezone.now(),
            is_active=True,
        )
        return user

    def is_anonymous_expired(self):
        if not self.is_anonymous or not self.anonymous_created_at:
            return False
        expiry = self.anonymous_created_at + getattr(settings, "ANONYMOUS_USER_LIFETIME", timedelta(days=30))
        return timezone.now() > expiry

    def upgrade_username_from_anonymous(self):
        """
        Upgrade username from anon_xxx to user_xxx when user verifies email/phone.
        Preserves uniqueness by keeping the same suffix.
        """
        if self.username and self.username.startswith('anon_'):
            suffix = self.username[5:]  # Extract part after 'anon_'
            new_username = f'user_{suffix}'
            # Check uniqueness and generate new suffix if needed
            while type(self).objects.filter(username=new_username).exclude(id=self.id).exists():
                new_username = f'user_{uuid.uuid4().hex[:8]}'
            self.username = new_username


class User(AbstractStapelUser):
    """Default concrete user (AUTH_USER_MODEL = "users.User")."""

    class Meta:
        app_label = "users"
        db_table = "users"
        indexes = [
            models.Index(fields=["email"], name="users_email_idx"),
            models.Index(fields=["phone"], name="users_phone_idx"),
            models.Index(fields=["oauth_provider", "oauth_id"], name="users_oauth_idx"),
        ]
