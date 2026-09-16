"""A contract nothing drives is a contract nothing checks.

Five of the first eight libraries swept with a wire test had a pytest urlconf
pointing somewhere their own ``docs/schema.json`` does not describe, so the
committed contract was never driven by anything and every suite was green
throughout. The shapes were all different — a prefix one segment short, the
paths mounted bare, less than the emission mounted, both the host's segment
and the module's own skipped, and a DOUBLED prefix — which is why the check
belongs in one place every library inherits rather than in eight conftests.

The doubled-prefix case is the instructive one, and it is pinned below: that
prefix is a REAL deployed prefix, pinned on purpose by another test in that
library. Two mounts answering two different questions is fine. Neither being
asked about the contract is not.
"""
import pytest
from django.urls import include, path

from stapel_core.testing import (
    assert_declared_paths_resolve,
    declared_paths,
    unresolved_declared_paths,
)

SCHEMA = {
    "paths": {
        "/thing/api/v1/widgets": {},
        "/thing/api/v1/widgets/{widget_id}": {},
    }
}


def _widget(request, widget_id=None):  # pragma: no cover - never called
    raise AssertionError("resolution only")


inner = [
    path("v1/widgets", _widget),
    path("v1/widgets/<str:widget_id>", _widget),
]

# The mount the document describes.
contract_urlpatterns = [path("thing/api/", include((inner, "thing")))]

# One host's real deployed prefix — a different question, pinned on purpose.
deployed_urlpatterns = [path("thing/api/thing/", include((inner, "thing")))]


class _Conf:
    def __init__(self, patterns):
        self.urlpatterns = patterns


CONTRACT = _Conf(contract_urlpatterns)
DEPLOYED = _Conf(deployed_urlpatterns)


def test_declared_paths_lists_every_path_sorted():
    assert declared_paths(SCHEMA) == [
        "/thing/api/v1/widgets",
        "/thing/api/v1/widgets/{widget_id}",
    ]


def test_the_contract_mount_resolves():
    assert unresolved_declared_paths(SCHEMA, urlconf=CONTRACT) == []
    assert_declared_paths_resolve(SCHEMA, urlconf=CONTRACT)


def test_a_doubled_prefix_is_caught_and_the_paths_are_named():
    """The stapel-workspaces shape: `thing/api/thing/` against a document
    written for `thing/api/`. Every declared path is unreachable at once,
    which is exactly what no per-operation check can see."""
    unresolved = unresolved_declared_paths(SCHEMA, urlconf=DEPLOYED)
    assert unresolved == declared_paths(SCHEMA)

    with pytest.raises(AssertionError) as caught:
        assert_declared_paths_resolve(SCHEMA, urlconf=DEPLOYED)
    message = str(caught.value)
    assert "a contract nothing drives is a contract nothing checks" in message
    for declared in declared_paths(SCHEMA):
        assert declared in message, "the failure must NAME the unresolved paths"


def test_a_library_can_pin_its_deployed_prefix_without_disabling_the_gate():
    """Both halves at once: the deployed mount is asserted on its own terms
    while the contract mount is asserted on the document's. A library with a
    different deployed prefix keeps both questions, and loses neither."""
    from django.urls import Resolver404, resolve

    resolve("/thing/api/thing/v1/widgets", urlconf=DEPLOYED)  # the deployed pin
    with pytest.raises(Resolver404):
        resolve("/thing/api/thing/v1/widgets", urlconf=CONTRACT)
    assert_declared_paths_resolve(SCHEMA, urlconf=CONTRACT)  # the contract pin


def test_an_integer_keyed_path_is_not_a_false_positive():
    """A single uuid candidate was the first cut and it reported three false
    positives on integer-keyed paths in the first library it ran on."""
    int_only = [path("thing/api/", include(([path("v1/n/<int:n>", _widget)], "thing")))]
    assert unresolved_declared_paths(
        {"paths": {"/thing/api/v1/n/{n}": {}}}, urlconf=_Conf(int_only)
    ) == []
