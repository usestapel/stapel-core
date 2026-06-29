"""
Cursor-based (anchor) pagination for DRF.

Provides efficient pagination for large datasets using cursor/anchor approach
instead of offset-based pagination. This is ideal for infinite scroll UIs.

Usage:
    from stapel_core.django.api.pagination import AnchorPagination

    class MyViewSet(viewsets.ModelViewSet):
        pagination_class = AnchorPagination

    # Or with custom settings:
    class MyPagination(AnchorPagination):
        page_size = 50
        anchor_field = 'created_at'
        ordering = '-created_at'
"""
from typing import Any, Dict, List, Optional
from rest_framework import serializers
from rest_framework.pagination import BasePagination
from rest_framework.response import Response
from rest_framework.request import Request
from django.db.models import QuerySet


class AnchorPagination(BasePagination):
    """
    Cursor-based pagination using an anchor field.

    Query params:
        - anchor: Value of anchor field to start after (exclusive)
        - limit: Number of items to return (default: page_size)
        - direction: 'next' (default), 'prev', or 'center' for bidirectional scrolling

    Response format:
        {
            "items": [...],
            "next_anchor": "2024-01-15T10:30:00Z",  # or null if no more
            "prev_anchor": "2024-01-15T09:00:00Z",  # or null if at start
            "has_next": true,
            "has_prev": false,
            "count": 20  # items in current page
        }

    Direction modes:
        - next: Items after anchor (default)
        - prev: Items before anchor
        - center: Items around anchor (anchor item + items before/after)

    Attributes:
        page_size: Default number of items per page (default: 20)
        max_page_size: Maximum allowed page size (default: 100)
        anchor_field: Model field to use as anchor (default: 'id')
        anchor_param: Query param name for anchor (default: 'anchor')
        limit_param: Query param name for limit (default: 'limit')
        direction_param: Query param name for direction (default: 'direction')
        ordering: Default ordering, must include anchor_field (default: '-id')
    """

    page_size = 100
    max_page_size = 1000
    anchor_field = 'id'
    anchor_param = 'anchor'
    limit_param = 'limit'
    direction_param = 'direction'
    ordering = '-id'

    def paginate_queryset(
        self,
        queryset: QuerySet,
        request: Request,
        view: Any = None
    ) -> Optional[List[Any]]:
        """
        Paginate queryset using anchor-based cursor.
        """
        self.request = request
        self.view = view

        # Get pagination params
        self.anchor = request.query_params.get(self.anchor_param)
        self.direction = request.query_params.get(self.direction_param, 'next')
        self.limit = self._get_limit(request)

        # Determine if ordering is descending
        is_descending = self.ordering.startswith('-')
        order_field = self.ordering.lstrip('-')

        # Store for response generation
        self._is_descending = is_descending
        self._order_field = order_field

        # Handle center direction separately
        if self.direction == 'center' and self.anchor:
            return self._paginate_center(queryset, order_field, is_descending)

        # Apply ordering
        queryset = queryset.order_by(self.ordering)

        # Apply anchor filter
        if self.anchor:
            if self.direction == 'prev':
                # Going backwards - reverse the comparison
                if is_descending:
                    queryset = queryset.filter(**{f'{order_field}__gt': self.anchor})
                else:
                    queryset = queryset.filter(**{f'{order_field}__lt': self.anchor})
            else:
                # Going forward (next)
                if is_descending:
                    queryset = queryset.filter(**{f'{order_field}__lt': self.anchor})
                else:
                    queryset = queryset.filter(**{f'{order_field}__gt': self.anchor})

        # Fetch one extra to check if there's more
        items = list(queryset[:self.limit + 1])

        # Check if there are more items
        if self.direction == 'prev':
            # When going backwards, reverse the results
            items = items[::-1]
            self._has_prev = len(items) > self.limit
            if self._has_prev:
                items = items[1:]  # Remove the extra item
            self._has_next = bool(self.anchor)  # If we have anchor, there's next
        else:
            self._has_next = len(items) > self.limit
            if self._has_next:
                items = items[:-1]  # Remove the extra item
            self._has_prev = bool(self.anchor)  # If we have anchor, there's prev

        self._items = items
        return items

    def _paginate_center(
        self,
        queryset: QuerySet,
        order_field: str,
        is_descending: bool
    ) -> List[Any]:
        """
        Paginate with anchor in the center of results.

        Gets items before and after the anchor, with anchor item included.
        """
        half_limit = self.limit // 2

        # Get items before anchor (in display order)
        if is_descending:
            before_qs = queryset.filter(
                **{f'{order_field}__gt': self.anchor}
            ).order_by(order_field)[:half_limit + 1]
            before_items = list(before_qs)[::-1]  # Reverse to get display order
        else:
            before_qs = queryset.filter(
                **{f'{order_field}__lt': self.anchor}
            ).order_by(f'-{order_field}')[:half_limit + 1]
            before_items = list(before_qs)[::-1]  # Reverse to get display order

        # Get anchor item itself
        anchor_item = queryset.filter(**{order_field: self.anchor}).first()

        # Get items after anchor (in display order)
        if is_descending:
            after_qs = queryset.filter(
                **{f'{order_field}__lt': self.anchor}
            ).order_by(self.ordering)[:half_limit + 1]
        else:
            after_qs = queryset.filter(
                **{f'{order_field}__gt': self.anchor}
            ).order_by(self.ordering)[:half_limit + 1]
        after_items = list(after_qs)

        # Check has_prev/has_next
        self._has_prev = len(before_items) > half_limit
        self._has_next = len(after_items) > half_limit

        # Trim extra items used for has_prev/has_next check
        if self._has_prev:
            before_items = before_items[1:]
        if self._has_next:
            after_items = after_items[:-1]

        # Combine: before + anchor + after
        items = before_items
        if anchor_item:
            items = items + [anchor_item]
        items = items + after_items

        self._items = items
        return items

    def _get_limit(self, request: Request) -> int:
        """Get limit from request, respecting max_page_size."""
        try:
            limit = int(request.query_params.get(self.limit_param, self.page_size))
            return min(limit, self.max_page_size)
        except (ValueError, TypeError):
            return self.page_size

    def get_paginated_response(self, data: List[Any]) -> Response:
        """
        Return paginated response with anchor metadata.
        """
        # Get anchor values from first/last items
        next_anchor = None
        prev_anchor = None

        if self._items:
            # Get the anchor field value from items
            first_item = self._items[0]
            last_item = self._items[-1]

            # Handle both dict and model objects
            if isinstance(last_item, dict):
                next_anchor = last_item.get(self._order_field) if self._has_next else None
                prev_anchor = first_item.get(self._order_field) if self._has_prev else None
            else:
                next_anchor = getattr(last_item, self._order_field, None) if self._has_next else None
                prev_anchor = getattr(first_item, self._order_field, None) if self._has_prev else None

        # Convert datetime to ISO string if needed
        if next_anchor and hasattr(next_anchor, 'isoformat'):
            next_anchor = next_anchor.isoformat()
        if prev_anchor and hasattr(prev_anchor, 'isoformat'):
            prev_anchor = prev_anchor.isoformat()

        return Response({
            'items': data,
            'next_anchor': next_anchor,
            'prev_anchor': prev_anchor,
            'has_next': self._has_next,
            'has_prev': self._has_prev,
            'count': len(data),
        })

    def get_schema_operation_parameters(self, auto_schema=None):
        return [
            {
                "name": self.anchor_param,
                "required": False,
                "in": "query",
                "description": "Anchor value to paginate from (exclusive)",
                "schema": {"type": "string"},
            },
            {
                "name": self.limit_param,
                "required": False,
                "in": "query",
                "description": f"Number of items (default {self.page_size}, max {self.max_page_size})",
                "schema": {"type": "integer"},
            },
            {
                "name": self.direction_param,
                "required": False,
                "in": "query",
                "description": "Pagination direction",
                "schema": {
                    "type": "string",
                    "enum": ["next", "prev", "center"],
                    "default": "next",
                },
            },
        ]

    def get_paginated_response_schema(self, schema: Dict) -> Dict:
        """
        Return OpenAPI schema for paginated response.
        """
        return {
            'type': 'object',
            'properties': {
                'items': schema,
                'next_anchor': {
                    'type': 'string',
                    'nullable': True,
                    'description': 'Anchor value for next page',
                },
                'prev_anchor': {
                    'type': 'string',
                    'nullable': True,
                    'description': 'Anchor value for previous page',
                },
                'has_next': {
                    'type': 'boolean',
                    'description': 'Whether there are more items after this page',
                },
                'has_prev': {
                    'type': 'boolean',
                    'description': 'Whether there are items before this page',
                },
                'count': {
                    'type': 'integer',
                    'description': 'Number of items in current page',
                },
            },
            'required': ['items', 'has_next', 'has_prev', 'count'],
        }


class AnchorPaginationSerializer(serializers.Serializer):
    """
    Serializer for anchor pagination response (for Swagger docs).

    Usage with drf-spectacular:
        @extend_schema(
            responses={200: AnchorPaginationSerializer(child=MyItemSerializer())}
        )
    """
    items = serializers.ListField(child=serializers.DictField())
    next_anchor = serializers.CharField(allow_null=True)
    prev_anchor = serializers.CharField(allow_null=True)
    has_next = serializers.BooleanField()
    has_prev = serializers.BooleanField()
    count = serializers.IntegerField()


# Convenience subclasses for common use cases

class CreatedAtAnchorPagination(AnchorPagination):
    """Anchor pagination by created_at (newest first)."""
    anchor_field = 'created_at'
    ordering = '-created_at'


class UpdatedAtAnchorPagination(AnchorPagination):
    """Anchor pagination by updated_at (newest first)."""
    anchor_field = 'updated_at'
    ordering = '-updated_at'


class IDAnchorPagination(AnchorPagination):
    """Anchor pagination by ID (newest first, assumes auto-increment)."""
    anchor_field = 'id'
    ordering = '-id'
