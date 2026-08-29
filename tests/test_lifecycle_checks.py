"""Tests for stapel_core.comm.lifecycle_checks.

The class this closes: stapel-auth 0.30.0 added ``user.merged`` (a guest
account folded into a survivor on sign-in), and every library that already
subscribed to ``user.deleted`` kept its rows pointed at the merged-away id
without anything raising. The check turns that silence into a boot error.
"""
from __future__ import annotations

import pytest

from stapel_core.comm import on_action, subscribe_action
from stapel_core.comm.lifecycle_checks import (
    E001_LIFECYCLE_PAIR_UNHANDLED,
    check_lifecycle_pairs,
)
from stapel_core.comm.registry import action_registry


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is a process singleton; these tests own it for their run."""
    action_registry.clear()
    yield
    action_registry.clear()


def _subscribe(action: str, module: str, name: str) -> None:
    """Subscribe a handler that claims to live in *module*."""

    def handler(event):  # pragma: no cover — never delivered here
        return None

    handler.__module__ = module
    handler.__name__ = name
    subscribe_action(action, handler)


def _ids(errors) -> list[str]:
    return [error.id for error in errors]


def test_deleted_without_merged_is_an_error():
    _subscribe("user.deleted", "stapel_billing.actions", "handle_user_deleted")

    errors = check_lifecycle_pairs()

    assert _ids(errors) == [E001_LIFECYCLE_PAIR_UNHANDLED]
    message = errors[0].msg
    assert "stapel_billing" in message
    assert "user.merged" in message
    assert "user.deleted" in message


def test_both_handlers_is_clean():
    _subscribe("user.deleted", "stapel_billing.actions", "handle_user_deleted")
    _subscribe("user.merged", "stapel_billing.actions", "handle_user_merged")

    assert check_lifecycle_pairs() == []


def test_neither_handler_is_clean():
    _subscribe("listing.published", "stapel_search.actions", "reindex")

    assert check_lifecycle_pairs() == []


def test_merged_alone_is_clean():
    """The pair is directional: merging without deleting is a valid stance."""
    _subscribe("user.merged", "stapel_chat.actions", "handle_user_merged")

    assert check_lifecycle_pairs() == []


def test_a_no_op_merged_handler_is_a_green_answer():
    """An app with nothing to re-parent declares it, and is green."""

    @on_action("user.deleted")
    def erase(event):  # pragma: no cover
        return None

    @on_action("user.merged")
    def nothing_to_reparent(event):  # pragma: no cover
        """No per-user rows here."""

    erase.__module__ = nothing_to_reparent.__module__ = "stapel_translate.actions"

    assert check_lifecycle_pairs() == []


def test_each_unpaired_app_is_reported_once():
    _subscribe("user.deleted", "stapel_billing.actions", "a")
    _subscribe("user.deleted", "stapel_billing.gdpr", "b")
    _subscribe("user.deleted", "stapel_profiles.actions", "c")

    errors = check_lifecycle_pairs()

    assert len(errors) == 2
    reported = sorted(error.msg.split("'")[1] for error in errors)
    assert reported == ["stapel_billing", "stapel_profiles"]


def test_handler_registered_on_a_library_behalf_is_charged_to_that_library():
    """The gdpr-owner closures live in core; the finding must not say core."""

    def core_built_closure(event):  # pragma: no cover
        return None

    core_built_closure.__module__ = "stapel_core.gdpr.owners"
    core_built_closure.stapel_handler_module = "stapel_calendar.apps"
    subscribe_action("user.deleted", core_built_closure)

    errors = check_lifecycle_pairs()

    assert len(errors) == 1
    assert "stapel_calendar" in errors[0].msg
    assert "stapel_core" not in errors[0].msg


def test_handler_is_attributed_to_the_installed_app_that_owns_its_module():
    _subscribe("user.deleted", "stapel_core.django.outbox.relay", "h")

    errors = check_lifecycle_pairs()

    assert len(errors) == 1
    # The longest matching AppConfig.name, not the top-level package.
    assert "'stapel_core.django.outbox'" in errors[0].msg


def test_app_configs_filter_is_honoured():
    from django.apps import apps as django_apps

    _subscribe("user.deleted", "stapel_billing.actions", "h")

    other = [
        config
        for config in django_apps.get_app_configs()
        if config.name == "stapel_core.django.outbox"
    ]
    assert other, "the outbox app must be installed for this test to mean anything"
    assert check_lifecycle_pairs(app_configs=other) == []


def test_register_gdpr_owner_stamps_the_calling_library():
    """The stamp the check relies on is set where core subscribes for others."""
    from stapel_core.gdpr.owners import _build

    registration = _build("billing", ("account",), lambda *a: None, True, "stapel_billing.apps")

    assert registration.handle_user_deleted.stapel_handler_module == "stapel_billing.apps"
    assert registration.handle_erasure_requested.stapel_handler_module == "stapel_billing.apps"
