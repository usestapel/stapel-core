"""Coverage tests for stapel_core.django.api.revision."""
from types import SimpleNamespace

from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from stapel_core.django.api.revision import (
    REVISION_PARAMETERS,
    MaxRevisionSerializer,
    RevisionPagination,
    RevisionResponseSerializer,
    RevisionViewSetMixin,
    make_revision_response_serializer,
    revision_list_schema,
)

_factory = APIRequestFactory()


def _req(**params):
    return Request(_factory.get("/", params))


class Row:
    def __init__(self, revision, deleted=False, active=True):
        self.id = revision
        self.revision = revision
        self.deleted = deleted
        self.active = active


class FakeManager:
    def __init__(self, max_rev=7, deleted_ids=(3, 4)):
        self._max_rev = max_rev
        self._deleted_ids = list(deleted_ids)

    def aggregate(self, **kwargs):
        return {"max_rev": self._max_rev}

    def filter(self, **kwargs):
        mgr = self

        class _Result:
            def values_list(self, *args, **kw):
                return list(mgr._deleted_ids)

        return _Result()


def make_model(name="Widget", manager=None, with_active=False):
    attrs = {"objects": manager if manager is not None else FakeManager()}
    if with_active:
        attrs["active"] = True
    return type(name, (), attrs)


class FakeQuerySet:
    def __init__(self, items, model=None):
        self._items = list(items)
        self.model = model or make_model()

    def order_by(self, field):
        desc = field.startswith("-")
        key = field.lstrip("-")
        return FakeQuerySet(
            sorted(self._items, key=lambda o: getattr(o, key), reverse=desc),
            model=self.model,
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
                if op == "gt":
                    return attr > raw
                if op == "lte":
                    return attr <= raw
                return attr == raw

            items = [o for o in items if match(o)]
        return FakeQuerySet(items, model=self.model)

    def count(self):
        return len(self._items)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, idx):
        return self._items[idx]

    def __iter__(self):
        return iter(self._items)


def _rows_qs(n=10, model=None):
    return FakeQuerySet([Row(i) for i in range(1, n + 1)], model=model)


class TestRevisionPagination:
    def test_filters_by_revision_range(self):
        p = RevisionPagination()
        result = p.paginate_queryset(
            _rows_qs(), _req(min_revision="2", max_revision="8")
        )
        assert [r.revision for r in result] == [3, 4, 5, 6, 7, 8]
        assert p.min_revision == 2
        assert p.max_revision == 8

    def test_invalid_params_ignored(self):
        p = RevisionPagination()
        result = p.paginate_queryset(
            _rows_qs(), _req(min_revision="abc", max_revision="xyz")
        )
        assert len(result) == 10
        assert p.min_revision is None
        assert p.max_revision is None

    def test_empty_max_revision_ignored(self):
        p = RevisionPagination()
        p.paginate_queryset(_rows_qs(), _req(max_revision=""))
        assert p.max_revision is None

    def test_no_params_returns_all(self):
        p = RevisionPagination()
        result = p.paginate_queryset(_rows_qs(), _req())
        assert [r.revision for r in result] == list(range(1, 11))

    def test_get_paginated_response(self):
        p = RevisionPagination()
        p.paginate_queryset(_rows_qs(), _req(min_revision="2"))
        resp = p.get_paginated_response(
            [{"revision": 3}, {"revision": 8}, {"no_rev": 1}]
        )
        assert resp.data["revisions"]["min"] == 3
        assert resp.data["revisions"]["max"] == 8
        assert resp.data["revisions"]["global_max"] == 7
        assert resp.data["revisions"]["deleted_ids"] == [3, 4]
        assert resp.data["pagination"]["page"] == 1
        assert resp.data["pagination"]["has_next"] is False
        assert resp.data["pagination"]["total_count"] == 8

    def test_get_paginated_response_empty_data(self):
        p = RevisionPagination()
        p.paginate_queryset(_rows_qs(), _req())
        resp = p.get_paginated_response([])
        assert resp.data["revisions"]["min"] is None
        assert resp.data["revisions"]["max"] is None
        assert resp.data["revisions"]["deleted_ids"] == []

    def test_data_without_revision_keys(self):
        p = RevisionPagination()
        p.paginate_queryset(_rows_qs(), _req())
        resp = p.get_paginated_response([{"x": 1}])
        assert resp.data["revisions"]["min"] is None

    def test_global_max_defaults_to_zero(self):
        model = make_model("Empty", manager=FakeManager(max_rev=None))
        p = RevisionPagination()
        p.paginate_queryset(_rows_qs(model=model), _req())
        resp = p.get_paginated_response([{"revision": 1}])
        assert resp.data["revisions"]["global_max"] == 0

    def test_paginated_response_schema(self):
        schema = RevisionPagination().get_paginated_response_schema({"type": "array"})
        assert schema["properties"]["results"] == {"type": "array"}
        assert "deleted_ids" in schema["properties"]["revisions"]["properties"]
        assert schema["required"] == ["pagination", "revisions", "results"]


class _BaseViewSet:
    def get_queryset(self):
        return self._qs


class _ViewSet(RevisionViewSetMixin, _BaseViewSet):
    pass


def _viewset(qs, **query_params):
    vs = _ViewSet()
    vs._qs = qs
    vs.queryset = qs
    vs.request = SimpleNamespace(query_params=query_params)
    return vs


class TestRevisionViewSetMixin:
    def test_get_queryset_excludes_deleted(self):
        qs = FakeQuerySet([Row(1), Row(2, deleted=True)])
        vs = _viewset(qs, include_deleted="false")
        assert [r.revision for r in vs.get_queryset()] == [1]

    def test_get_queryset_includes_deleted_by_default(self):
        qs = FakeQuerySet([Row(1), Row(2, deleted=True)])
        vs = _viewset(qs)
        assert len(list(vs.get_queryset())) == 2

    def test_revision_action(self):
        vs = _viewset(_rows_qs())
        resp = vs.revision(None)
        assert resp.data == {"revision": 7}

    def test_revision_action_defaults_to_zero(self):
        model = make_model("Bare", manager=FakeManager(max_rev=None))
        vs = _viewset(_rows_qs(model=model))
        resp = vs.revision(None)
        assert resp.data == {"revision": 0}

    def test_data_json_requires_revision_param(self):
        vs = _viewset(_rows_qs())
        resp = vs.data_json(SimpleNamespace(query_params={}))
        assert resp.status_code == 400
        assert "error" in resp.data

    def test_data_json_cache_miss_then_hit(self):
        model = make_model("CacheMissModel")
        vs = _viewset(_rows_qs(model=model))
        calls = []

        def get_serializer(qs, many=False):
            calls.append(len(list(qs)))
            return SimpleNamespace(data=[{"a": 1}])

        vs.get_serializer = get_serializer
        request = SimpleNamespace(query_params={"revision": "7"})

        resp = vs.data_json(request)
        assert resp.status_code == 200
        assert resp.data == [{"a": 1}]
        assert resp["Cache-Control"] == "public, max-age=2592000"
        assert len(calls) == 1

        # Second call served from cache — serializer not called again
        resp2 = vs.data_json(request)
        assert resp2.data == [{"a": 1}]
        assert len(calls) == 1

    def test_data_json_filters_active(self):
        model = make_model("ActiveModel", with_active=True)
        rows = [Row(1, active=True), Row(2, active=False), Row(3, deleted=True)]
        vs = _viewset(FakeQuerySet(rows, model=model))
        seen = {}

        def get_serializer(qs, many=False):
            seen["revisions"] = [r.revision for r in qs]
            return SimpleNamespace(data=[])

        vs.get_serializer = get_serializer
        vs.data_json(SimpleNamespace(query_params={"revision": "1"}))
        assert seen["revisions"] == [1]

    def test_cache_key_uses_model_name(self):
        vs = _viewset(_rows_qs(model=make_model("MyThing")))
        assert vs._get_data_json_cache_key() == "revision_data_json_mything"

    def test_current_max_revision(self):
        vs = _viewset(_rows_qs())
        assert vs._get_current_max_revision() == 7


class _ItemSerializer(serializers.Serializer):
    name = serializers.CharField()


class TestResponseSerializers:
    def test_revision_parameters_names(self):
        names = [p.name for p in REVISION_PARAMETERS]
        assert names == [
            "min_revision",
            "max_revision",
            "include_deleted",
            "page_size",
            "page",
        ]

    def test_max_revision_serializer(self):
        ser = MaxRevisionSerializer(data={"revision": 5})
        assert ser.is_valid()
        assert ser.validated_data["revision"] == 5

    def test_base_revision_response_serializer_fields(self):
        assert set(RevisionResponseSerializer().fields) == {
            "pagination",
            "revisions",
            "results",
        }

    def test_make_serializer_with_custom_name(self):
        cls = make_revision_response_serializer(_ItemSerializer, "ItemsPage")
        assert cls.__name__ == "ItemsPage"
        fields = cls().fields
        assert set(fields) == {"pagination", "revisions", "results"}
        assert set(fields["pagination"].fields) == {
            "page",
            "page_size",
            "total_pages",
            "total_count",
            "has_next",
            "has_previous",
        }
        assert set(fields["revisions"].fields) == {
            "min",
            "max",
            "global_max",
            "deleted_ids",
        }

    def test_make_serializer_default_name(self):
        cls = make_revision_response_serializer(_ItemSerializer)
        assert cls.__name__ == "_ItemSerializerRevisionResponse"


class TestRevisionListSchema:
    def test_wraps_existing_list(self):
        class Base:
            def list(self, request, *args, **kwargs):
                return "original"

        @revision_list_schema(_ItemSerializer, response_name="Page1")
        class VS(Base):
            pass

        assert VS().list(None) == "original"

    def test_falls_back_to_super_when_no_list(self):
        class Base:
            pass

        @revision_list_schema(_ItemSerializer)
        class VS(Base):
            pass

        # list added to the base only after decoration — decorator saw no list
        Base.list = lambda self, request, *args, **kwargs: "from-base"
        assert VS().list(None) == "from-base"
