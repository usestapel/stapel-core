"""
DRF components for revision-based synchronization.

This module provides pagination and viewset mixins for models using RevisionMixin.
Clients can efficiently sync data by requesting only changes since their last known revision.
"""

from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from django.db.models import Max
from django.core.cache import cache
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiExample
from drf_spectacular.types import OpenApiTypes


class RevisionPagination(PageNumberPagination):
    """
    Pagination class for revision-based sync.

    Query parameters:
    - min_revision: Return objects with revision > min_revision (default: None - no filter, return all)
    - max_revision: Return objects with revision <= max_revision (default: None - no limit)
    - page_size: Number of items per page (default: 100, max: 1000)
    - page: Page number (default: 1)

    Sync flow:
    1. Initial sync: GET /endpoint/ (no min_revision) - returns ALL items including revision=0
    2. Store revisions.global_max from response
    3. Next sync: GET /endpoint/?min_revision={stored_max} - returns only changes with revision > stored_max

    Response structure:
    {
        "pagination": {
            "page": 1,
            "page_size": 100,
            "total_pages": 5,
            "total_count": 450,
            "has_next": true,
            "has_previous": false
        },
        "revisions": {
            "min": 101,        # Min revision in current response
            "max": 200,        # Max revision in current response
            "global_max": 500  # Global max revision in the table
        },
        "results": [...]
    }
    """

    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 1000

    def paginate_queryset(self, queryset, request, view=None):
        """Filter queryset by revision range before pagination."""
        # Get revision parameters
        min_revision = request.query_params.get('min_revision', None)
        max_revision = request.query_params.get('max_revision', None)

        try:
            self.min_revision = int(min_revision) if min_revision is not None else None
        except (ValueError, TypeError):
            self.min_revision = None

        try:
            self.max_revision = int(max_revision) if max_revision else None
        except (ValueError, TypeError):
            self.max_revision = None

        # Store the model class for global_max calculation
        self.model_class = queryset.model

        # Filter by revision range (only if min_revision is specified)
        # min_revision=0 means "get items with revision > 0" (skip unsynced legacy data)
        # No min_revision means "get all items including revision=0"
        if self.min_revision is not None:
            queryset = queryset.filter(revision__gt=self.min_revision)
        if self.max_revision is not None:
            queryset = queryset.filter(revision__lte=self.max_revision)

        # Order by revision for consistent pagination
        queryset = queryset.order_by('revision')

        # Store for response metadata
        self.filtered_queryset = queryset

        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        """Return response with revision metadata."""
        # Calculate revision stats from the current page data
        if data:
            page_revisions = [item.get('revision', 0) for item in data if 'revision' in item]
            response_min = min(page_revisions) if page_revisions else None
            response_max = max(page_revisions) if page_revisions else None
        else:
            response_min = None
            response_max = None

        # Get global max revision
        global_max = self.model_class.objects.aggregate(
            max_rev=Max('revision')
        )['max_rev'] or 0

        # Get deleted IDs above the min_revision
        deleted_ids = []
        if self.min_revision is not None:
            deleted_ids = list(
                self.model_class.objects.filter(
                    deleted=True,
                    revision__gt=self.min_revision
                ).values_list('id', flat=True)
            )

        return Response({
            'pagination': {
                'page': self.page.number,
                'page_size': self.page.paginator.per_page,
                'total_pages': self.page.paginator.num_pages,
                'total_count': self.page.paginator.count,
                'has_next': self.page.has_next(),
                'has_previous': self.page.has_previous(),
            },
            'revisions': {
                'min': response_min,
                'max': response_max,
                'global_max': global_max,
                'deleted_ids': deleted_ids,
            },
            'results': data,
        })

    def get_paginated_response_schema(self, schema):
        """
        Return OpenAPI schema for revision-paginated response.
        This tells drf-spectacular to use our custom response structure.
        """
        return {
            'type': 'object',
            'required': ['pagination', 'revisions', 'results'],
            'properties': {
                'pagination': {
                    'type': 'object',
                    'properties': {
                        'page': {'type': 'integer', 'description': 'Current page number'},
                        'page_size': {'type': 'integer', 'description': 'Number of items per page'},
                        'total_pages': {'type': 'integer', 'description': 'Total number of pages'},
                        'total_count': {'type': 'integer', 'description': 'Total number of items'},
                        'has_next': {'type': 'boolean', 'description': 'Whether there is a next page'},
                        'has_previous': {'type': 'boolean', 'description': 'Whether there is a previous page'},
                    },
                    'required': ['page', 'page_size', 'total_pages', 'total_count', 'has_next', 'has_previous'],
                },
                'revisions': {
                    'type': 'object',
                    'properties': {
                        'min': {'type': 'integer', 'nullable': True, 'description': 'Minimum revision in current response'},
                        'max': {'type': 'integer', 'nullable': True, 'description': 'Maximum revision in current response'},
                        'global_max': {'type': 'integer', 'description': 'Global maximum revision in the table'},
                        'deleted_ids': {
                            'type': 'array',
                            'items': {'type': 'integer'},
                            'description': 'IDs of soft-deleted items since min_revision',
                        },
                    },
                    'required': ['min', 'max', 'global_max', 'deleted_ids'],
                },
                'results': schema,
            },
        }


# OpenAPI parameters for revision endpoints
REVISION_PARAMETERS = [
    OpenApiParameter(
        name='min_revision',
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        description='Return objects with revision greater than this value (exclusive). Omit for initial sync to get all items.',
        required=False,
    ),
    OpenApiParameter(
        name='max_revision',
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        description='Return objects with revision less than or equal to this value (inclusive). Default: no limit',
        required=False,
    ),
    OpenApiParameter(
        name='include_deleted',
        type=OpenApiTypes.BOOL,
        location=OpenApiParameter.QUERY,
        description='Include soft-deleted items in response. Default: true',
        required=False,
        default=True,
    ),
    OpenApiParameter(
        name='page_size',
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        description='Number of items per page. Default: 100, Max: 1000',
        required=False,
        default=100,
    ),
    OpenApiParameter(
        name='page',
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        description='Page number. Default: 1',
        required=False,
        default=1,
    ),
]


class MaxRevisionSerializer(serializers.Serializer):
    """Serializer for /revision endpoint response."""
    revision = serializers.IntegerField(help_text='Current maximum revision number')


class RevisionViewSetMixin:
    """
    Mixin for ViewSets working with RevisionMixin models.

    Provides:
    - Automatic revision-based filtering
    - OpenAPI documentation for revision query parameters
    - Optional filtering of soft-deleted items
    - /revision endpoint to get current max revision

    Usage:
        class MyModelViewSet(RevisionViewSetMixin, viewsets.ModelViewSet):
            queryset = MyModel.objects.all()
            serializer_class = MyModelSerializer
            pagination_class = RevisionPagination

    Query parameters:
    - min_revision: Return objects with revision > min_revision (default: 0)
    - max_revision: Return objects with revision <= max_revision (default: None)
    - include_deleted: Include soft-deleted items (default: true)
    """

    # Backwards compatibility alias
    revision_parameters = REVISION_PARAMETERS

    def get_queryset(self):
        """
        Filter queryset based on include_deleted parameter.

        Note: min_revision and max_revision filtering is handled by RevisionPagination.
        """
        queryset = super().get_queryset()

        # Check include_deleted parameter
        include_deleted = self.request.query_params.get('include_deleted', 'true')
        if include_deleted.lower() in ('false', '0', 'no'):
            queryset = queryset.filter(deleted=False)

        return queryset

    @extend_schema(
        description='Get the current maximum revision number for this resource.',
        responses={200: MaxRevisionSerializer},
    )
    @action(detail=False, methods=['get'], pagination_class=None)
    def revision(self, _request):
        """Return the current maximum revision number."""
        model_class = self.queryset.model  # type: ignore[attr-defined]
        max_rev = model_class.objects.aggregate(max_rev=Max('revision'))['max_rev'] or 0
        return Response({'revision': max_rev})

    def _get_data_json_cache_key(self):
        """Get cache key for data.json endpoint."""
        model_name = self.queryset.model.__name__.lower()  # type: ignore[attr-defined]
        return f'revision_data_json_{model_name}'

    def _get_current_max_revision(self):
        """Get current max revision for the model."""
        model_class = self.queryset.model  # type: ignore[attr-defined]
        return model_class.objects.aggregate(max_rev=Max('revision'))['max_rev'] or 0

    @extend_schema(
        description='''Get all data as a cacheable JSON array.

The `revision` parameter is required for cache busting - clients should use the current
max revision from `/revision` endpoint. Response includes Cache-Control header for 30 days.

**Usage:**
1. Call `/revision` to get current max revision
2. Call `/data.json?revision={max_revision}` to get all data
3. Cache the response locally - it won't change until revision changes
''',
        parameters=[
            OpenApiParameter(
                name='revision',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description='Current revision number (for cache busting). Get from /revision endpoint.',
                required=True,
                examples=[
                    OpenApiExample(
                        'Current revision',
                        value=1,
                        description='Use the value from /revision endpoint',
                    ),
                ],
            ),
        ],
    )
    @action(detail=False, methods=['get'], url_path='data.json', pagination_class=None)
    def data_json(self, request):
        """Return all items as a JSON array with long cache headers."""
        # Validate revision parameter is present
        revision_param = request.query_params.get('revision')
        if revision_param is None:
            return Response(
                {'error': 'revision query parameter is required'},
                status=400
            )

        # Try to get from Redis cache (5 minutes)
        cache_key = self._get_data_json_cache_key()
        cached_data = cache.get(cache_key)

        if cached_data is None:
            # Get only active, non-deleted items
            queryset = self.queryset.filter(deleted=False)  # type: ignore[attr-defined]
            if hasattr(self.queryset.model, 'active'):
                queryset = queryset.filter(active=True)
            serializer = self.get_serializer(queryset, many=True)  # type: ignore[attr-defined]
            cached_data = serializer.data
            # Cache in Redis for 5 minutes
            cache.set(cache_key, cached_data, timeout=300)

        response = Response(cached_data)
        # Set Cache-Control for 30 days (browser/CDN caching)
        response['Cache-Control'] = 'public, max-age=2592000'
        return response


class RevisionResponseSerializer(serializers.Serializer):
    """
    Base serializer for revision-paginated responses.

    Subclass this and override get_results_serializer() to type the results field.
    """
    pagination = serializers.DictField(
        child=serializers.IntegerField(),
        help_text='Pagination info: page, page_size, total_pages, total_count, has_next, has_previous'
    )
    revisions = serializers.DictField(
        child=serializers.IntegerField(allow_null=True),
        help_text='Revision info: min, max, global_max'
    )
    results = serializers.ListField(
        child=serializers.DictField(),
        help_text='List of items'
    )


def make_revision_response_serializer(item_serializer_class, name=None):
    """
    Create a typed response serializer for revision-paginated endpoints.

    Args:
        item_serializer_class: The serializer class for individual items in results.
        name: Optional custom name for the response serializer (e.g., 'CategoriesPage').
              If not provided, defaults to '{ItemSerializer}RevisionResponse'.

    Returns:
        A new serializer class with properly typed results field.

    Usage:
        CategoriesPageResponse = make_revision_response_serializer(CategorySerializer, 'CategoriesPage')
    """
    class PaginationSerializer(serializers.Serializer):
        page = serializers.IntegerField()
        page_size = serializers.IntegerField()
        total_pages = serializers.IntegerField()
        total_count = serializers.IntegerField()
        has_next = serializers.BooleanField()
        has_previous = serializers.BooleanField()

    class RevisionsSerializer(serializers.Serializer):
        min = serializers.IntegerField(allow_null=True, help_text='Minimum revision in current response')
        max = serializers.IntegerField(allow_null=True, help_text='Maximum revision in current response')
        global_max = serializers.IntegerField(help_text='Global maximum revision in the table')
        deleted_ids = serializers.ListField(
            child=serializers.IntegerField(),
            help_text='IDs of soft-deleted items since min_revision'
        )

    class TypedRevisionResponseSerializer(serializers.Serializer):
        pagination = PaginationSerializer()
        revisions = RevisionsSerializer()
        results = item_serializer_class(many=True)

    # Set a unique name for OpenAPI
    if name:
        TypedRevisionResponseSerializer.__name__ = name
    else:
        TypedRevisionResponseSerializer.__name__ = f'{item_serializer_class.__name__}RevisionResponse'

    return TypedRevisionResponseSerializer


def revision_list_schema(serializer_class, response_name=None):
    """
    Decorator to add revision-based list schema to a ViewSet's list action.

    Args:
        serializer_class: The serializer class for individual items in results.
        response_name: Optional custom name for the response schema (e.g., 'CategoriesPage').

    Usage:
        @revision_list_schema(MySerializer, response_name='ItemsPage')
        class MyViewSet(RevisionViewSetMixin, viewsets.ModelViewSet):
            ...
    """
    response_serializer = make_revision_response_serializer(serializer_class, response_name)

    def decorator(viewset_class):
        original_list = viewset_class.list if hasattr(viewset_class, 'list') else None

        @extend_schema(
            description='''List objects with revision-based pagination.

**Synchronization flow:**
1. Initial sync: Call with no parameters to get all data
2. Store the `revisions.global_max` from response
3. Subsequent syncs: Call with `min_revision={stored_max}` to get only changes
4. Handle items with `deleted=true` by removing them locally

**Response includes:**
- `pagination`: Page info (page, page_size, total_pages, total_count)
- `revisions`: Revision metadata (min, max in response, global_max in table)
- `results`: Array of items ordered by revision
''',
            parameters=REVISION_PARAMETERS,
            responses={200: response_serializer},
        )
        def list(self, request, *args, **kwargs):
            if original_list:
                return original_list(self, request, *args, **kwargs)
            return super(viewset_class, self).list(request, *args, **kwargs)

        viewset_class.list = list
        return viewset_class

    return decorator
