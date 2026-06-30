"""Backward-compatibility re-exports for openapi module."""
from stapel_core.django.api.errors import IronErrorSerializer  # noqa: F401
from stapel_core.django.openapi.schemas import (  # noqa: F401
    extend_schema,
    get_error_responses,
)
