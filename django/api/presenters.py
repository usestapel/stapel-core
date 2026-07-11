"""DAO→DTO presenter primitive (§55 slice 1 — extensibility without lock-in).

Precedent (``tasks/research-django-extensibility.md`` §4): django-allauth's
headless mode already ships "host dataclass + serialize function" for its
user payload; ``djangorestframework-dataclasses`` (a stapel-core dependency
already) generalizes the dataclass→DRF-serializer half, with native
drf-spectacular support. This module is the thin layer the governing spec
(``docs/pending/extensibility-presenters.md`` §2) asks for on top of both:

- a :class:`Presenter` subclass declares a Django model, a tuple of fields
  taken **as-is** (type + description come from the DAO model field) and a
  dict of :class:`PresenterField` **custom fields** (rename/transform/
  compute — type + description come from the field spec itself, since there
  is no passthrough DAO field to source them from);
- ``present(dao) -> dto`` builds one instance of the auto-generated ``dto``
  dataclass (``cls.dto``, available after the subclass is created);
- ``serializer_class()`` gives the drf-spectacular-visible
  :class:`~stapel_core.django.api.serializers.StapelDataclassSerializer` for
  that dto — OpenAPI field descriptions and a whole-object example (parsed
  from the presenter's own docstring, see below) come along for free;
- several presenters may target the same model — each is its own read view,
  nothing here is exclusive;
- a custom field's ``type`` may be another ``Presenter`` (optionally with
  ``many=True``) to nest a presented sub-object; the DTO field then
  resolves to that presenter's own ``dto`` dataclass, and ``present()``
  recurses into it.

Document the presented shape with a class-docstring ``Example:`` block — a
JSON object literal, parsed at class-creation time and attached to the
generated serializer as an OpenAPI example::

    class UserProfilePresenter(Presenter):
        '''Presents a User row as the public profile view.

        Example:
            {
                "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "email": "user@example.com",
                "display_name": "Alice"
            }
        '''
        model = User
        fields = ("id", "email")
        custom_fields = {
            "display_name": PresenterField(
                type=str, source=lambda dao: dao.username,
                help_text="Public display name (falls back to the username).",
            ),
        }

Swap: a Presenter subclass is replaced wholesale via config — never
subclassed-and-monkeypatched in place, never imported directly by consuming
library code. See :mod:`stapel_core.django.swappable`
(``get_presenter("KEY", default="pkg.mod.DefaultPresenter")``).
"""
from __future__ import annotations

import dataclasses
import datetime
import decimal
import json
import re
import textwrap
import uuid
from typing import Any, Callable, Union

from django.db import models
from django.db.models.fields.related import ForeignKey, OneToOneField

from stapel_core.django.api.serializers import StapelDataclassSerializer, _attach_example

#: Sentinel distinguishing "no example supplied" from ``example=None``.
_UNSET = object()

FieldSource = Union[str, Callable[[Any], Any], None]


@dataclasses.dataclass(frozen=True)
class PresenterField:
    """One custom (non-passthrough) field on a :class:`Presenter`.

    Attributes:
        type: the DTO field's Python type annotation. May be another
            ``Presenter`` subclass (or paired with ``many=True`` for a
            collection) to nest a presented sub-object — the DTO field then
            resolves to that presenter's own ``dto`` dataclass.
        source: attribute name read off the DAO (defaults to the DTO field
            name), or a ``callable(dao) -> value`` for a computed/renamed
            value.
        help_text: OpenAPI field description. This is the *only* source of
            a description for a custom field — there is no DAO field to
            fall back to (§2 of the governing spec).
        many: True when ``source`` yields an iterable of DAO rows to
            present through a nested ``Presenter`` (``type`` must then be a
            ``Presenter`` subclass).
        default: default value for the DTO field. Unset (the default) means
            the field is required — a real dataclass field default of
            ``None`` is written as ``default=None`` explicitly.
        example: optional field-level example, attached via dataclass field
            metadata (picked up by ``StapelDataclassSerializer``).
    """

    type: Any = str
    source: FieldSource = None
    help_text: str = ""
    many: bool = False
    default: Any = _UNSET
    example: Any = _UNSET


# Django model field class -> Python type, for "as-is" fields. Order does
# not disambiguate here: every subclass of a mapped type maps to the same
# Python type (e.g. BigAutoField < IntegerField -> int either way).
_TYPE_MAP: dict[type, type] = {
    models.BooleanField: bool,
    models.UUIDField: uuid.UUID,
    models.DateTimeField: datetime.datetime,
    models.DateField: datetime.date,
    models.TimeField: datetime.time,
    models.DurationField: datetime.timedelta,
    models.DecimalField: decimal.Decimal,
    models.FloatField: float,
    models.JSONField: Any,
    models.IntegerField: int,
    models.CharField: str,
    models.TextField: str,
}


def _infer_type(field: models.Field) -> Any:
    """Best-effort Python type for a Django model field (as-is fields only)."""
    if isinstance(field, (ForeignKey, OneToOneField)):
        return _infer_type(field.target_field)
    for django_type, py_type in _TYPE_MAP.items():
        if isinstance(field, django_type):
            return py_type
    return Any


def _field_help_text(field: models.Field) -> str:
    text = field.help_text or field.verbose_name or ""
    return str(text)


_EXAMPLE_HEADER_RE = re.compile(r"^[ \t]*Example:[ \t]*$", re.MULTILINE)


def _parse_presenter_docstring(docstring: str) -> tuple[str, Any]:
    """Split a presenter docstring into (description, example-or-None).

    ``description`` is everything before an ``Example:`` section (or the
    whole docstring, dedented, if there is none). ``example`` is the JSON
    value parsed out of the indented block following ``Example:`` — ``None``
    if there is no such section or it doesn't parse as JSON.
    """
    if not docstring:
        return "", None
    text = textwrap.dedent(docstring).strip("\n")
    # dedent() only strips whitespace common to *every* line; a normal class
    # docstring's first line starts at column 0 already (right after the
    # opening quotes) while the rest carry the method-body indent — dedent
    # already accounts for that as long as blank lines don't interfere.
    text = "\n".join(line.rstrip() for line in text.splitlines())

    match = _EXAMPLE_HEADER_RE.search(text)
    if not match:
        return text.strip(), None

    description = text[: match.start()].strip()
    body = textwrap.dedent(text[match.end():]).strip()
    try:
        example = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        example = None
    return description, example


class Presenter:
    """Base DAO→DTO presenter. See the module docstring for the full contract."""

    model: type[models.Model]
    fields: tuple[str, ...] = ()
    custom_fields: dict[str, PresenterField] = {}

    # Populated by __init_subclass__ / _build_dto.
    dto: type
    _field_sources: dict[str, FieldSource]
    _nested: dict[str, tuple[type["Presenter"], bool]]
    _presenter_example: Any = None

    _serializer_cache: dict[type, type] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Intermediate abstract subclasses (no model declared yet) get no dto.
        if getattr(cls, "model", None) is None:
            return
        cls._build_dto()

    @classmethod
    def _build_dto(cls) -> None:
        description, example = _parse_presenter_docstring(cls.__doc__ or "")
        cls._presenter_example = example

        dc_fields: list[tuple[str, Any, dataclasses.Field]] = []
        sources: dict[str, FieldSource] = {}
        nested: dict[str, tuple[type["Presenter"], bool]] = {}

        for name in cls.fields:
            try:
                model_field = cls.model._meta.get_field(name)
            except Exception as exc:
                raise TypeError(
                    f"{cls.__name__}.fields references {name!r}, which is not "
                    f"a field on {cls.model!r}"
                ) from exc
            py_type = _infer_type(model_field)
            help_text = _field_help_text(model_field)
            metadata = {"help_text": help_text} if help_text else {}
            dc_fields.append((name, py_type, dataclasses.field(metadata=metadata)))
            sources[name] = name

        for name, spec in cls.custom_fields.items():
            py_type = spec.type
            if isinstance(py_type, type) and issubclass(py_type, Presenter):
                nested[name] = (py_type, spec.many)
                py_type = list[py_type.dto] if spec.many else py_type.dto  # type: ignore[valid-type]

            metadata: dict[str, Any] = {}
            if spec.help_text:
                metadata["help_text"] = spec.help_text
            if spec.example is not _UNSET:
                metadata["example"] = spec.example

            field_kwargs: dict[str, Any] = {"metadata": metadata}
            if spec.default is not _UNSET:
                field_kwargs["default"] = spec.default
            dc_fields.append((name, py_type, dataclasses.field(**field_kwargs)))
            sources[name] = spec.source if spec.source is not None else name

        # dataclass rule: fields without a default must precede fields with
        # one. Stable sort keeps declaration order within each group.
        def _has_default(entry: tuple[str, Any, dataclasses.Field]) -> bool:
            fld = entry[2]
            return not (
                fld.default is dataclasses.MISSING
                and fld.default_factory is dataclasses.MISSING
            )

        dc_fields.sort(key=_has_default)

        dto = dataclasses.make_dataclass(f"{cls.__name__}DTO", dc_fields, frozen=True)
        dto.__doc__ = description
        cls.dto = dto
        cls._field_sources = sources
        cls._nested = nested

    @classmethod
    def present(cls, dao: Any) -> Any:
        """Build one ``cls.dto`` instance from a DAO row (the DAO→DTO map)."""
        values: dict[str, Any] = {}
        for name, source in cls._field_sources.items():
            raw = source(dao) if callable(source) else getattr(dao, source)
            nested_spec = cls._nested.get(name)
            if nested_spec is not None:
                presenter_cls, many = nested_spec
                if many:
                    raw = [presenter_cls.present(item) for item in raw] if raw is not None else []
                elif raw is not None:
                    raw = presenter_cls.present(raw)
            values[name] = raw
        return cls.dto(**values)

    @classmethod
    def present_many(cls, daos: Any) -> list:
        """``present()`` mapped over an iterable of DAO rows."""
        return [cls.present(dao) for dao in daos]

    @classmethod
    def serializer_class(cls) -> type:
        """A cached :class:`StapelDataclassSerializer` subclass for ``cls.dto``
        (§2 schema generation): field descriptions come from ``cls.dto``'s
        field metadata (as-is → DAO model field; custom → ``PresenterField``);
        the whole-object example comes from the presenter's own docstring.
        """
        cached = Presenter._serializer_cache.get(cls)
        if cached is not None:
            return cached

        meta = type("Meta", (), {"dataclass": cls.dto})
        ser_cls = type(
            f"{cls.__name__}Serializer",
            (StapelDataclassSerializer,),
            {"Meta": meta, "__module__": cls.__module__},
        )
        if cls._presenter_example is not None:
            _attach_example(ser_cls, cls.dto.__name__, cls._presenter_example)
        Presenter._serializer_cache[cls] = ser_cls
        return ser_cls


__all__ = ["Presenter", "PresenterField"]
