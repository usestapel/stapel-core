"""
Common OpenAPI schemas and utilities for drf-spectacular.

This module provides reusable response serializers, error schemas,
and helper functions for consistent API documentation across services.
"""

from drf_spectacular.openapi import AutoSchema
from drf_spectacular.plumbing import get_override
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import serializers

from stapel_core.django.api.errors import StapelErrorSerializer

# =============================================================================
# Custom AutoSchema with Permission Classes Display
# =============================================================================


class PermissionAwareAutoSchema(AutoSchema):
    """
    AutoSchema that includes permission classes in operation description.

    This automatically appends permission class names to the description
    of each endpoint in Swagger/OpenAPI documentation.
    """

    def _get_permission_info(self) -> str:
        """Get formatted permission classes string."""
        view = self.view
        permission_classes = getattr(view, "permission_classes", [])

        if not permission_classes:
            return ""

        permission_names = [
            p.__name__ if isinstance(p, type) else p.__class__.__name__
            for p in permission_classes
        ]
        permissions_str = ", ".join(permission_names)
        return f"\n\n**Permissions:** `{permissions_str}`"

    def _get_serializer_field_meta(self, field, direction):
        meta = super()._get_serializer_field_meta(field, direction)
        example = get_override(field, "example")
        if example is not None:
            meta["example"] = example
        return meta

    def get_operation(self, path, path_regex, path_prefix, method, registry):
        """Override to append permissions after all description processing."""
        operation = super().get_operation(
            path, path_regex, path_prefix, method, registry
        )

        if operation is None:
            return None

        # Append permission info to the final description
        permission_info = self._get_permission_info()
        if permission_info and operation.get("description"):
            operation["description"] = operation["description"] + permission_info
        elif permission_info:
            operation["description"] = permission_info.strip()

        return operation


# =============================================================================
# Common Success Response Serializers
# =============================================================================


class MessageResponseSerializer(serializers.Serializer):
    """Simple message response."""

    message = serializers.CharField(help_text="Success message")


class BulkUpdateResponseSerializer(serializers.Serializer):
    """Response for bulk create/update operations."""

    updated_ids = serializers.ListField(
        child=serializers.UUIDField(), help_text="List of created/updated object IDs"
    )


class TokenResponseSerializer(serializers.Serializer):
    """JWT token response."""

    access = serializers.CharField(help_text="JWT access token")
    refresh = serializers.CharField(help_text="JWT refresh token")


# =============================================================================
# OpenAPI Response Helpers
# =============================================================================

# Pre-built OpenAPI responses for common HTTP status codes
COMMON_RESPONSES = {
    400: OpenApiResponse(
        response=StapelErrorSerializer,
        description="Bad Request - Validation error",
    ),
    401: OpenApiResponse(
        response=StapelErrorSerializer,
        description="Unauthorized - Authentication required",
    ),
    403: OpenApiResponse(
        response=StapelErrorSerializer,
        description="Forbidden - Permission denied",
    ),
    404: OpenApiResponse(
        response=StapelErrorSerializer,
        description="Not Found - Resource does not exist",
    ),
    409: OpenApiResponse(
        response=StapelErrorSerializer,
        description="Conflict - Resource already exists",
    ),
    422: OpenApiResponse(
        response=StapelErrorSerializer,
        description="Unprocessable Entity - Rate limit exceeded or blocked",
    ),
    500: OpenApiResponse(
        response=StapelErrorSerializer,
        description="Internal Server Error",
    ),
}


def get_error_responses(*status_codes: int) -> dict:
    """
    Get a dict of OpenAPI responses for the given status codes.

    Usage:
        @extend_schema(
            responses={
                200: MySuccessSerializer,
                **get_error_responses(400, 401, 404)
            }
        )
    """
    return {
        code: COMMON_RESPONSES[code]
        for code in status_codes
        if code in COMMON_RESPONSES
    }


# =============================================================================
# OpenAPI Examples
# =============================================================================

# Common examples for authentication endpoints
AUTH_EXAMPLES = {
    "email_request": OpenApiExample(
        name="Email verification request",
        value={"email": "user@example.com", "device_id": "device-12345"},
        request_only=True,
    ),
    "email_verify": OpenApiExample(
        name="Email verification",
        value={"email": "user@example.com", "code": "123456"},
        request_only=True,
    ),
    "phone_request": OpenApiExample(
        name="Phone verification request",
        value={"phone": "+12345678900", "device_id": "device-12345"},
        request_only=True,
    ),
    "phone_verify": OpenApiExample(
        name="Phone verification",
        value={"phone": "+12345678900", "code": "1234"},
        request_only=True,
    ),
    "auth_success_registered": OpenApiExample(
        name="New user registered",
        value={
            "status": "REGISTERED",
            "user": {"id": "uuid", "email": "user@example.com"},
            "tokens": {"access": "jwt...", "refresh": "jwt..."},
        },
        response_only=True,
    ),
    "auth_success_logged_in": OpenApiExample(
        name="Existing user logged in",
        value={
            "status": "LOGGED_IN",
            "user": {"id": "uuid", "email": "user@example.com"},
            "tokens": {"access": "jwt...", "refresh": "jwt..."},
        },
        response_only=True,
    ),
    "rate_limit_error": OpenApiExample(
        name="Rate limit exceeded",
        value={
            "localizable_error": "error.429.rate_limit",
            "error": "Too many attempts. Try again in 1 minutes.",
            "params": {
                "retry_after": 30,
                "retry_after_minutes": 1,
                "retry_after_display": "0:30",
            },
        },
        response_only=True,
    ),
    "blocked_error": OpenApiExample(
        name="Account blocked",
        value={
            "localizable_error": "error.422.blocked",
            "error": "Account temporarily blocked. Try again in 10 minutes.",
            "params": {
                "retry_after": 600,
                "retry_after_minutes": 10,
                "retry_after_display": "10:00",
            },
        },
        response_only=True,
    ),
}


# =============================================================================
# Decorator Helpers
# =============================================================================


def extend_schema_with_errors(*error_codes: int, **kwargs):
    """
    Decorator that wraps extend_schema and automatically adds common error responses.

    Usage:
        @extend_schema_with_errors(400, 401, 404,
            description='Get user by ID',
            responses={200: UserSerializer}
        )
        def retrieve(self, request, pk):
            ...
    """

    def decorator(func):
        responses = kwargs.pop("responses", {})
        responses.update(get_error_responses(*error_codes))
        return extend_schema(responses=responses, **kwargs)(func)

    return decorator


# =============================================================================
# drf-spectacular Hooks for Tag Organization
# =============================================================================


def preprocess_exclude_schema_endpoints(endpoints):
    """
    Preprocessing hook to exclude internal schema endpoints from documentation.

    Excludes:
    - /schema/ endpoints (except feature schema endpoints)
    - /.well-known/ discovery endpoints (JWKS, OpenID Configuration)
    """
    return [
        (path, path_regex, method, callback)
        for path, path_regex, method, callback in endpoints
        # Exclude .well-known discovery endpoints
        if "/.well-known/" not in path
    ]


def postprocess_schema_tags(result, generator, request, public):
    """
    Postprocessing hook to organize endpoints into semantic tag groups.

    Groups endpoints by the first path segment after /api/ and formats tag names
    to be human-readable (e.g., 'categories' -> 'Categories', 'api-keys' -> 'API Keys').
    """
    # Define tag descriptions for common groups
    tag_descriptions = {
        "auth": "Authentication and authorization endpoints",
        "categories": "Category management and feature configuration",
        "features": "Feature type definitions and schemas",
        "ads": "Advertisement management",
        "locations": "Geographic locations",
        "translations": "Internationalization and translations",
        "token": "JWT token management",
        "api-keys": "Service API key management",
        "files": "File upload and management",
    }

    # Collect all unique tags from paths
    tags_in_schema = set()

    for path, methods in result.get("paths", {}).items():
        for method, operation in methods.items():
            if isinstance(operation, dict) and "tags" in operation:
                tags_in_schema.update(operation["tags"])

    # Build tags list with descriptions
    tags_list = []
    for tag in sorted(tags_in_schema):
        tag_entry = {"name": tag}
        # Try to find description by lowercase tag name
        tag_lower = tag.lower().replace(" ", "-")
        if tag_lower in tag_descriptions:
            tag_entry["description"] = tag_descriptions[tag_lower]
        tags_list.append(tag_entry)

    if tags_list:
        result["tags"] = tags_list

    return result


def postprocess_fix_polymorphic_discriminators(result, generator, request, public):
    """
    Fix discriminator mappings for PolymorphicProxySerializer schemas.

    drf-spectacular uses serializer class names as discriminator mapping keys
    (e.g. 'AuthResponse' -> '...AuthResponse'). This hook replaces those keys
    with the actual enum values read from each sub-schema's discriminator field,
    so API generators (orval, openapi-typescript) emit correct union types.

    Convention: only patches schemas whose discriminator field is an enum —
    non-enum discriminators are left untouched.
    """
    schemas = result.get("components", {}).get("schemas", {})

    for schema_name, schema in schemas.items():
        if "oneOf" not in schema:
            continue
        discriminator = schema.get("discriminator")
        if not discriminator or "mapping" not in discriminator:
            continue

        prop_name = discriminator["propertyName"]
        new_mapping = {}

        for ref_obj in schema["oneOf"]:
            ref = ref_obj.get("$ref", "")
            if not ref:
                continue
            # '#/components/schemas/AuthResponse' → 'AuthResponse'
            sub_name = ref.rsplit("/", 1)[-1]
            sub_schema = schemas.get(sub_name, {})

            # Find the discriminator field inside the sub-schema
            prop_schema = sub_schema.get("properties", {}).get(prop_name, {})

            # Resolve $ref if the property itself is a $ref to an enum
            if "$ref" in prop_schema:
                enum_name = prop_schema["$ref"].rsplit("/", 1)[-1]
                prop_schema = schemas.get(enum_name, {})

            enum_values = prop_schema.get("enum", [])
            for val in enum_values:
                # All values → same ref (multiple status values can map to one schema)
                new_mapping[str(val)] = ref

        if new_mapping:
            discriminator["mapping"] = new_mapping

    return result


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # AutoSchema
    "PermissionAwareAutoSchema",
    # Unified error format
    "StapelErrorSerializer",
    # Success serializers
    "MessageResponseSerializer",
    "BulkUpdateResponseSerializer",
    "TokenResponseSerializer",
    # Helpers
    "COMMON_RESPONSES",
    "get_error_responses",
    "AUTH_EXAMPLES",
    "extend_schema_with_errors",
    # drf-spectacular hooks
    "preprocess_exclude_schema_endpoints",
    "postprocess_schema_tags",
    # Re-exports from drf-spectacular
    "extend_schema",
    "OpenApiParameter",
    "OpenApiExample",
    "OpenApiResponse",
    "OpenApiTypes",
]
