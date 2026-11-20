"""An item example of a paginated list is documented as a page, not an item.

drf-spectacular wraps an item example into the paginator's envelope only when
every envelope property carries an ``example``; otherwise it falls back to the
bare item, and the operation documents a page that fails its own schema.
"""
from dataclasses import dataclass

import pytest
from drf_spectacular.generators import SchemaGenerator
from rest_framework.views import APIView

from stapel_core.django.api.pagination import (
    CreatedAtAnchorPagination,
    IDAnchorPagination,
    UpdatedAtAnchorPagination,
)
from stapel_core.django.api.revision import RevisionPagination
from stapel_core.django.api.serializers import StapelDataclassSerializer
from stapel_core.django.openapi.schemas import PermissionAwareAutoSchema


@dataclass
class PageItem:
    """One row.

    Attributes:
        id: Row id. Example: 7
        title: Row title. Example: first
    """

    id: int
    title: str


class PageItemSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = PageItem


ITEM = {"id": 7, "title": "first"}

ANCHOR_PAGE = {
    "items": [ITEM],
    "next_anchor": None,
    "prev_anchor": None,
    "has_next": False,
    "has_prev": False,
    "count": 1,
}
REVISION_PAGE = {
    "pagination": {
        "page": 1, "page_size": 20, "total_pages": 1, "total_count": 1,
        "has_next": False, "has_previous": False,
    },
    "revisions": {"min": 1, "max": 1, "global_max": 1, "deleted_ids": []},
    "results": [ITEM],
}


def _operation_example(paginator):
    from django.urls import path

    from drf_spectacular.utils import extend_schema

    class ListView(APIView):
        schema = PermissionAwareAutoSchema()
        authentication_classes = []
        pagination_class = paginator

        @extend_schema(responses=PageItemSerializer(many=True))
        def get(self, request):  # pragma: no cover - never called
            raise NotImplementedError

    generator = SchemaGenerator(patterns=[path("rows/", ListView.as_view())])
    schema = generator.get_schema(request=None, public=True)
    content = schema["paths"]["/rows/"]["get"]["responses"]["200"]["content"]
    (example,) = content["application/json"]["examples"].values()
    return example["value"]


@pytest.mark.parametrize(
    "paginator",
    [CreatedAtAnchorPagination, UpdatedAtAnchorPagination, IDAnchorPagination],
)
def test_an_anchor_page_example_is_the_envelope(paginator):
    assert _operation_example(paginator) == ANCHOR_PAGE


def test_a_revision_page_example_is_the_envelope():
    assert _operation_example(RevisionPagination) == REVISION_PAGE
