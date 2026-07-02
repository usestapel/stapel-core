"""Coverage tests for stapel_core.django.api.pagination."""
import datetime

from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from stapel_core.django.api.pagination import (
    AnchorPagination,
    AnchorPaginationSerializer,
    CreatedAtAnchorPagination,
    IDAnchorPagination,
    UpdatedAtAnchorPagination,
)

_factory = APIRequestFactory()


def _req(**params):
    return Request(_factory.get("/", params))


class Item:
    def __init__(self, id):
        self.id = id

    def __repr__(self):
        return f"Item({self.id})"


def _coerce(value, sample):
    """Coerce a query-param string to the type of the compared attribute."""
    if isinstance(sample, int) and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


class FakeQuerySet:
    """Minimal list-backed stand-in supporting order_by/filter/slice/first."""

    def __init__(self, items):
        self._items = list(items)

    def order_by(self, field):
        desc = field.startswith("-")
        key = field.lstrip("-")
        return FakeQuerySet(
            sorted(self._items, key=lambda o: getattr(o, key), reverse=desc)
        )

    def filter(self, **kwargs):
        items = self._items
        for lookup, raw in kwargs.items():
            if "__" in lookup:
                fname, op = lookup.split("__", 1)
            else:
                fname, op = lookup, "exact"

            def match(o, fname=fname, op=op, raw=raw):
                attr = getattr(o, fname)
                value = _coerce(raw, attr)
                if op == "gt":
                    return attr > value
                if op == "lt":
                    return attr < value
                if op == "gte":
                    return attr >= value
                if op == "lte":
                    return attr <= value
                return attr == value

            items = [o for o in items if match(o)]
        return FakeQuerySet(items)

    def first(self):
        return self._items[0] if self._items else None

    def count(self):
        return len(self._items)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, idx):
        return self._items[idx]

    def __iter__(self):
        return iter(self._items)


class SmallPagination(AnchorPagination):
    page_size = 3
    max_page_size = 5


class AscPagination(AnchorPagination):
    page_size = 3
    max_page_size = 5
    ordering = "id"


def _qs(ids=range(1, 11)):
    return FakeQuerySet([Item(i) for i in ids])


class TestPaginateNextPrev:
    def test_next_no_anchor_desc(self):
        p = SmallPagination()
        items = p.paginate_queryset(_qs(), _req())
        assert [i.id for i in items] == [10, 9, 8]
        assert p._has_next is True
        assert p._has_prev is False

    def test_next_with_anchor_desc(self):
        p = SmallPagination()
        items = p.paginate_queryset(_qs(), _req(anchor="8"))
        assert [i.id for i in items] == [7, 6, 5]
        assert p._has_next is True
        assert p._has_prev is True

    def test_next_with_anchor_asc(self):
        p = AscPagination()
        items = p.paginate_queryset(_qs(), _req(anchor="2"))
        assert [i.id for i in items] == [3, 4, 5]
        assert p._has_next is True

    def test_next_last_page(self):
        p = SmallPagination()
        items = p.paginate_queryset(_qs(), _req(anchor="3"))
        assert [i.id for i in items] == [2, 1]
        assert p._has_next is False
        assert p._has_prev is True

    def test_prev_with_anchor_desc(self):
        p = SmallPagination()
        items = p.paginate_queryset(_qs(), _req(anchor="3", direction="prev"))
        assert [i.id for i in items] == [8, 9, 10]
        assert p._has_prev is True
        assert p._has_next is True

    def test_prev_with_anchor_asc(self):
        p = AscPagination()
        items = p.paginate_queryset(_qs(), _req(anchor="8", direction="prev"))
        assert [i.id for i in items] == [3, 2, 1]
        assert p._has_prev is True

    def test_prev_at_start(self):
        p = SmallPagination()
        items = p.paginate_queryset(_qs(), _req(anchor="9", direction="prev"))
        assert [i.id for i in items] == [10]
        assert p._has_prev is False
        assert p._has_next is True


class TestPaginateCenter:
    def test_center_desc(self):
        p = SmallPagination()
        items = p.paginate_queryset(
            _qs(), _req(anchor="5", direction="center", limit="4")
        )
        assert [i.id for i in items] == [7, 6, 5, 4, 3]
        assert p._has_prev is True
        assert p._has_next is True

    def test_center_asc(self):
        p = AscPagination()
        items = p.paginate_queryset(
            _qs(), _req(anchor="5", direction="center", limit="4")
        )
        assert [i.id for i in items] == [3, 4, 5, 6, 7]
        assert p._has_prev is True
        assert p._has_next is True

    def test_center_anchor_item_missing(self):
        p = SmallPagination()
        qs = _qs([1, 2, 3, 4, 6, 7, 8])
        items = p.paginate_queryset(
            qs, _req(anchor="5", direction="center", limit="4")
        )
        assert 5 not in [i.id for i in items]

    def test_center_no_prev_next(self):
        p = SmallPagination()
        items = p.paginate_queryset(
            _qs([4, 5, 6]), _req(anchor="5", direction="center", limit="4")
        )
        assert [i.id for i in items] == [6, 5, 4]
        assert p._has_prev is False
        assert p._has_next is False


class TestGetLimit:
    def test_invalid_limit_falls_back_to_page_size(self):
        p = SmallPagination()
        assert p._get_limit(_req(limit="abc")) == 3

    def test_limit_capped_at_max(self):
        p = SmallPagination()
        assert p._get_limit(_req(limit="100")) == 5

    def test_valid_limit(self):
        p = SmallPagination()
        assert p._get_limit(_req(limit="2")) == 2


class TestGetPaginatedResponse:
    def test_model_objects(self):
        p = SmallPagination()
        p.paginate_queryset(_qs(), _req(anchor="8"))
        resp = p.get_paginated_response(["a", "b", "c"])
        assert resp.data["next_anchor"] == 5
        assert resp.data["prev_anchor"] == 7
        assert resp.data["has_next"] is True
        assert resp.data["has_prev"] is True
        assert resp.data["count"] == 3

    def test_no_next_anchor_when_no_more(self):
        p = SmallPagination()
        p.paginate_queryset(_qs(), _req())
        resp = p.get_paginated_response(["a", "b", "c"])
        assert resp.data["prev_anchor"] is None
        assert resp.data["next_anchor"] == 8

    def test_dict_items_with_datetime(self):
        p = SmallPagination()
        dt1 = datetime.datetime(2024, 1, 1, 10, 0, 0)
        dt2 = datetime.datetime(2024, 1, 2, 10, 0, 0)
        p._items = [{"created_at": dt2}, {"created_at": dt1}]
        p._order_field = "created_at"
        p._has_next = True
        p._has_prev = True
        resp = p.get_paginated_response(p._items)
        assert resp.data["next_anchor"] == dt1.isoformat()
        assert resp.data["prev_anchor"] == dt2.isoformat()

    def test_empty_items(self):
        p = SmallPagination()
        p._items = []
        p._order_field = "id"
        p._has_next = False
        p._has_prev = False
        resp = p.get_paginated_response([])
        assert resp.data["next_anchor"] is None
        assert resp.data["prev_anchor"] is None
        assert resp.data["count"] == 0


class TestSchemaHelpers:
    def test_schema_operation_parameters(self):
        params = SmallPagination().get_schema_operation_parameters()
        names = [p["name"] for p in params]
        assert names == ["anchor", "limit", "direction"]
        assert params[2]["schema"]["enum"] == ["next", "prev", "center"]

    def test_paginated_response_schema(self):
        schema = SmallPagination().get_paginated_response_schema({"type": "array"})
        assert schema["properties"]["items"] == {"type": "array"}
        assert set(schema["required"]) == {"items", "has_next", "has_prev", "count"}

    def test_serializer_fields(self):
        fields = AnchorPaginationSerializer().fields
        assert set(fields) == {
            "items",
            "next_anchor",
            "prev_anchor",
            "has_next",
            "has_prev",
            "count",
        }


class TestConvenienceSubclasses:
    def test_created_at(self):
        assert CreatedAtAnchorPagination.anchor_field == "created_at"
        assert CreatedAtAnchorPagination.ordering == "-created_at"

    def test_updated_at(self):
        assert UpdatedAtAnchorPagination.anchor_field == "updated_at"
        assert UpdatedAtAnchorPagination.ordering == "-updated_at"

    def test_id(self):
        assert IDAnchorPagination.anchor_field == "id"
        assert IDAnchorPagination.ordering == "-id"
