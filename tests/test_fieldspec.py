"""#133: FieldSpec — a copy seam must classify every field of its model.

Generalized from a product seam (recurring meeting series → materialized
occurrence) where a hand-written list of "fields to inherit" carried two of
four settings fields. The two it dropped changed behaviour in the opposite
direction from the declared intent — an open series slammed the door on the
first join, a PIN series produced PIN-less rooms — and nothing failed until
users noticed.
"""
import pytest
from django.db import models

from stapel_core.django.fieldspec import FieldSpec, FieldSpecError


class SeamRoom(models.Model):
    """Stand-in for the product model the mechanism was lifted from."""

    code = models.CharField(max_length=32)
    title = models.CharField(max_length=64, blank=True)
    access_level = models.CharField(max_length=16, default="open")
    admit_required = models.BooleanField(default=True)
    admit_required_default = models.BooleanField(default=True)
    pin_code = models.CharField(max_length=8, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "tests"

    FIELD_SPEC = FieldSpec(
        copy=("access_level", "admit_required", "admit_required_default", "pin_code"),
        recompute=("id", "code", "title"),
        never=("created_at",),
    )


def test_exhaustive_partition_validates():
    SeamRoom.FIELD_SPEC.validate(SeamRoom)


def test_values_returns_exactly_the_copy_half():
    master = SeamRoom(
        code="abc", title="Weekly", access_level="trusted",
        admit_required=False, admit_required_default=False, pin_code="424242",
    )
    values = SeamRoom.FIELD_SPEC.values(master)
    assert values == {
        "access_level": "trusted",
        "admit_required": False,
        "admit_required_default": False,
        "pin_code": "424242",
    }
    # The seam's own fields are the caller's business, not the spec's.
    assert "code" not in values and "created_at" not in values


def test_unassigned_field_is_named():
    """The defect the mechanism exists for: a new settings field that nobody
    classified. Proven the way the product proved it — by planting one."""
    spec = FieldSpec(
        copy=("access_level", "admit_required", "admit_required_default"),
        recompute=("id", "code", "title"),
        never=("created_at",),
    )
    with pytest.raises(FieldSpecError) as exc:
        spec.validate(SeamRoom)
    assert "pin_code" in str(exc.value)
    assert "neither copy, recompute nor never" in str(exc.value)


def test_values_refuses_to_build_from_an_incomplete_spec():
    """Loud at the seam, not only in a test that someone remembered to write."""
    spec = FieldSpec(copy=("access_level",), recompute=("id",), never=())
    with pytest.raises(FieldSpecError):
        spec.values(SeamRoom(access_level="open"))


def test_declared_name_that_is_not_a_field():
    spec = FieldSpec(
        copy=("access_level", "admit_required", "admit_required_default", "pin_code"),
        recompute=("id", "code", "title", "renamed_away"),
        never=("created_at",),
    )
    with pytest.raises(FieldSpecError) as exc:
        spec.validate(SeamRoom)
    assert "renamed_away" in str(exc.value)


def test_field_in_two_lists_is_not_a_decision():
    spec = FieldSpec(
        copy=("access_level", "admit_required", "admit_required_default", "pin_code"),
        recompute=("id", "code", "title", "created_at"),
        never=("created_at",),
    )
    with pytest.raises(FieldSpecError) as exc:
        spec.validate(SeamRoom)
    assert "more than one list" in str(exc.value)
    assert "created_at" in str(exc.value)


def test_a_wrong_but_explicit_verdict_passes():
    """The documented boundary. Classifying pin_code as 'never' is a security
    regression and the mechanism cannot know that — it enforces that the
    author decided, not that the decision was right."""
    spec = FieldSpec(
        copy=("access_level", "admit_required", "admit_required_default"),
        recompute=("id", "code", "title"),
        never=("created_at", "pin_code"),
    )
    spec.validate(SeamRoom)
    assert "pin_code" not in spec.values(SeamRoom(pin_code="424242"))
