"""
Common admin configurations for User model and RevisionMixin.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _


class SuperuserOnlyMixin:
    """
    Admin mixin that restricts access to superusers only.

    Use this for models containing sensitive data like:
    - User personal information
    - Payment/billing data
    - Security configurations
    - API keys and secrets

    Usage:
        @admin.register(SensitiveModel)
        class SensitiveModelAdmin(SuperuserOnlyMixin, admin.ModelAdmin):
            list_display = ['id', 'name']
    """

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class SuperuserOnlyAdmin(SuperuserOnlyMixin, admin.ModelAdmin):
    """
    Standalone admin class that restricts access to superusers only.

    Usage:
        @admin.register(SensitiveModel)
        class SensitiveModelAdmin(SuperuserOnlyAdmin):
            list_display = ['id', 'name']
    """
    pass


class RevisionAdmin(admin.ModelAdmin):
    """
    Admin mixin for models using RevisionMixin.

    Provides:
    - Display of revision and deleted fields
    - Actions for soft delete and restore
    - Filtering by deleted status

    Usage:
        @admin.register(MyModel)
        class MyModelAdmin(RevisionAdmin):
            list_display = ['name', 'revision', 'deleted']
    """

    list_display_revision = ['revision', 'deleted']
    list_filter_revision = ['deleted']
    readonly_fields_revision = ['revision']

    def get_list_display(self, request):
        """Add revision fields to list display."""
        list_display = list(super().get_list_display(request))
        for field in self.list_display_revision:
            if field not in list_display:
                list_display.append(field)
        return list_display

    def get_list_filter(self, request):
        """Add deleted filter."""
        list_filter = list(super().get_list_filter(request))
        for field in self.list_filter_revision:
            if field not in list_filter:
                list_filter.append(field)
        return list_filter

    def get_readonly_fields(self, request, obj=None):
        """Make revision field readonly."""
        readonly_fields = list(super().get_readonly_fields(request, obj))
        for field in self.readonly_fields_revision:
            if field not in readonly_fields:
                readonly_fields.append(field)
        return readonly_fields

    actions = ['mark_deleted', 'restore_deleted']

    @admin.action(description=_('Mark selected items as deleted'))
    def mark_deleted(self, request, queryset):
        """Soft delete selected objects."""
        count = 0
        for obj in queryset:
            if not obj.deleted:
                obj.soft_delete()
                count += 1
        self.message_user(request, _(f'{count} item(s) marked as deleted.'))

    @admin.action(description=_('Restore selected items'))
    def restore_deleted(self, request, queryset):
        """Restore soft-deleted objects."""
        count = 0
        for obj in queryset:
            if obj.deleted:
                obj.restore()
                count += 1
        self.message_user(request, _(f'{count} item(s) restored.'))


class UserAdmin(BaseUserAdmin):
    """
    Common User admin for all services.

    Access controlled via Django group permissions.
    Add users to 'Staff' group to grant access.
    """

    list_display = ['id', 'email', 'username', 'phone', 'auth_type', 'is_anonymous', 'is_active', 'created_at']
    list_filter = ['auth_type', 'is_anonymous', 'is_active', 'is_email_verified', 'is_phone_verified']
    search_fields = ['email', 'username', 'phone']
    ordering = ['-created_at']

    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        (_('Personal info'), {'fields': ('username', 'phone', 'avatar', 'bio')}),
        (_('Authentication'), {'fields': ('auth_type', 'is_email_verified', 'is_phone_verified', 'is_anonymous', 'anonymous_created_at')}),
        (_('OAuth'), {'fields': ('oauth_provider', 'oauth_id')}),
        (_('Permissions'), {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        (_('Important dates'), {'fields': ('last_login', 'created_at', 'updated_at')}),
    )

    readonly_fields = ['id', 'created_at', 'updated_at']

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'username', 'password1', 'password2'),
        }),
    )
