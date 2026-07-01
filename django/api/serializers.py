"""
Base serializers for Stapel Django services.
"""
import dataclasses
import re

from rest_framework_dataclasses.fields import EnumField
from rest_framework_dataclasses.serializers import DataclassSerializer

# Serializer field kwargs that DataclassSerializer doesn't read from metadata.
_FIELD_META_KEYS = {'help_text', 'label', 'allow_null', 'required', 'default'}

# Regex for "Attributes:" / "Members:" docstring sections (Google-style).
_SECTION_RE = re.compile(r'^\s{0,4}(Attributes|Members)\s*:', re.MULTILINE)
_ATTR_LINE_RE = re.compile(r'^\s{4,8}(\w+)\s*(?:\(.*?\))?\s*:\s*(.+)$')
_EXAMPLE_RE = re.compile(r'^(.*?)\.\s*Example:\s*(.+)$')


class StapelDataclassSerializer(DataclassSerializer):
    """
    DataclassSerializer that bridges dataclass/enum docstrings to OpenAPI.

    All documentation lives in the DTO docstrings — no ``field(metadata=…)``
    needed (though it still works as an override).

    Features:

    1. **Schema description** — first paragraph of the dataclass docstring
       becomes the OpenAPI component description.

    2. **Field descriptions & examples** — parsed from ``Attributes:`` section::

           @dataclass
           class MyResponse:
               '''Human-readable schema description.

               Attributes:
                   name: Display name. Example: Alice
                   status: Current status.
               '''
               name: str
               status: str

    3. **Enum per-value descriptions** — parsed from ``Members:`` section::

           class Status(str, Enum):
               '''Status codes.

               Members:
                   ACTIVE: Currently active.
                   INACTIVE: Disabled by admin.
               '''
               ACTIVE = 'ACTIVE'
               INACTIVE = 'INACTIVE'

    4. **field(metadata=…) override** — ``help_text``, ``label``, ``example``
       from metadata take precedence over docstring values.
    """

    @property
    def serializer_dataclass_field(self):
        return StapelDataclassSerializer

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Auto-generated nested instances (no subclass, no __init_subclass__ hook).
        # Set the dataclass docstring as the OpenAPI component description.
        if type(self) is StapelDataclassSerializer:
            dc = self.dataclass_definition.dataclass_type
            parsed = _parse_docstring(dc.__doc__ or '')
            if parsed.get('description'):
                annotation = dict(getattr(self, '_spectacular_annotation', None) or {})
                annotation['description'] = parsed['description']
                self._spectacular_annotation = annotation

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        dc = getattr(getattr(cls, 'Meta', None), 'dataclass', None)
        if not dc or not dataclasses.is_dataclass(dc):
            return

        parsed = _parse_docstring(dc.__doc__ or '')

        # Schema description (prevents MRO fallback to parent docstring)
        cls.__doc__ = parsed['description'] or ''

        # Store parsed attrs/members for get_fields()
        cls._stapel_doc = parsed

        # Build example from docstring + metadata overrides
        example_value = {}
        for f in dataclasses.fields(dc):
            # metadata example takes precedence
            if f.metadata and 'example' in f.metadata:
                example_value[f.name] = f.metadata['example']
            elif f.name in parsed['attributes'] and parsed['attributes'][f.name].get('example'):
                example_value[f.name] = parsed['attributes'][f.name]['example']
        if example_value:
            _attach_example(cls, dc.__name__, example_value)

    def get_fields(self):
        fields = super().get_fields()
        dc = self.dataclass_definition.dataclass_type

        # 1. Apply docstring-parsed help_text
        # For explicit subclasses, _stapel_doc is set by __init_subclass__.
        # For auto-generated nested instances, parse at runtime.
        parsed = getattr(type(self), '_stapel_doc', None)
        if parsed is None:
            parsed = _parse_docstring(dc.__doc__ or '')
        doc_attrs = parsed.get('attributes', {})
        for name, info in doc_attrs.items():
            if name in fields:
                if info.get('help_text'):
                    fields[name].help_text = info['help_text']
                if info.get('example'):
                    _set_field_example(fields[name], info['example'])

        # 2. field(metadata=…) overrides docstring values
        meta_by_name = {f.name: f.metadata for f in dataclasses.fields(dc) if f.metadata}
        for name, meta in meta_by_name.items():
            if name in fields:
                for key in _FIELD_META_KEYS:
                    if key in meta:
                        setattr(fields[name], key, meta[key])
                if 'example' in meta:
                    _set_field_example(fields[name], meta['example'])

        # 3. Enum choice labels from docstring Members section
        doc_members = parsed.get('members', {})
        for name, ser_field in fields.items():
            if isinstance(ser_field, EnumField):
                _apply_enum_descriptions(ser_field, doc_members)

        return fields


# ---------------------------------------------------------------------------
# Docstring parser
# ---------------------------------------------------------------------------

def _parse_docstring(docstring):
    """
    Parse Google-style docstring into description, attributes, and members.

    Returns::

        {
            'description': 'First paragraph',
            'attributes': {'field': {'help_text': '...', 'example': '...'}},
            'members': {'MEMBER': 'Description'},
        }
    """
    result = {'description': '', 'attributes': {}, 'members': {}}
    if not docstring:
        return result

    lines = docstring.expandtabs(4).splitlines()

    # Find section boundaries
    section_starts = []
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            section_starts.append((i, m.group(1)))

    # Description = everything before first section
    desc_end = section_starts[0][0] if section_starts else len(lines)
    desc_lines = [line.strip() for line in lines[:desc_end]]
    result['description'] = '\n'.join(desc_lines).strip()

    # Parse each section
    for idx, (start, section_name) in enumerate(section_starts):
        end = section_starts[idx + 1][0] if idx + 1 < len(section_starts) else len(lines)
        body_lines = lines[start + 1:end]

        entries = {}
        for line in body_lines:
            m = _ATTR_LINE_RE.match(line)
            if m:
                field_name, text = m.group(1), m.group(2).strip()
                entries[field_name] = text

        if section_name == 'Attributes':
            for field_name, text in entries.items():
                info = {}
                em = _EXAMPLE_RE.match(text)
                if em:
                    info['help_text'] = em.group(1).strip()
                    info['example'] = _coerce_example(em.group(2).strip())
                else:
                    info['help_text'] = text.rstrip('.')
                result['attributes'][field_name] = info

        elif section_name == 'Members':
            for member_name, text in entries.items():
                result['members'][member_name] = text.rstrip('.')

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_enum_descriptions(ser_field, doc_members):
    """Override enum choice labels with descriptions from docstring or member.description."""
    enum_cls = ser_field.enum_class
    members = list(enum_cls)
    if not members:
        return

    # Source 1: enum docstring Members section
    if not doc_members:
        enum_parsed = _parse_docstring(enum_cls.__doc__ or '')
        doc_members = enum_parsed.get('members', {})

    # Source 2: member.description attribute (from __new__ pattern)
    has_attr_desc = hasattr(members[0], 'description') and any(
        getattr(m, 'description', None) for m in members
    )

    if not doc_members and not has_attr_desc:
        return

    # Build new choices as list of tuples (ChoiceField setter requires this)
    new_choices = []
    for m in enum_cls:
        key = ser_field.to_representation(m)
        label = doc_members.get(m.name) or getattr(m, 'description', None) or m.name
        new_choices.append((key, label))

    ser_field.choices = new_choices


def _coerce_example(value):
    """Try to parse example string as JSON (for dicts, lists, numbers, booleans)."""
    import json
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def _set_field_example(ser_field, value):
    """Set per-field example via drf-spectacular annotation."""
    annotation = dict(getattr(ser_field, '_spectacular_annotation', None) or {})
    annotation['example'] = value
    ser_field._spectacular_annotation = annotation


def _attach_example(cls, name, example_value):
    """Attach auto-generated OpenApiExample to the serializer class."""
    try:
        from drf_spectacular.utils import OpenApiExample
    except ImportError:
        return
    annotation = dict(getattr(cls, '_spectacular_annotation', None) or {})
    examples = list(annotation.get('examples') or [])
    examples.append(OpenApiExample(name=name, value=example_value, response_only=True))
    annotation['examples'] = examples
    cls._spectacular_annotation = annotation
