"""The tenancy seam's shipped default stops answering yes to a guest.

Three modules ship a ``DefaultScopeProvider`` that answers every tenancy
question with yes and is documented "swap in production". Nothing made the
swap happen, so the shipped answer was also the deployed one. What is pinned
here: the shipped defaults now refuse a provably mandate-less caller wherever
the deployment can ask, a non-answer refuses with 503 instead of admitting,
and a genuinely standalone host keeps its single-tenant semantics.
"""
import pytest
from django.core import checks
from django.core.cache import cache
from django.test import override_settings

from stapel_core.comm import function_registry
from stapel_core.django.api.permissions import MandateUnavailable
from stapel_core.django.mandate import MANDATE_FUNCTION, MANDATE_RESULT_KEY
from stapel_core.django.scope import (
    MandateScopeMixin,
    check_shipped_scope_provider,
    deployment_is_standalone,
)


class FakeUser:
    def __init__(self, pk="u-1"):
        self.pk = pk
        self.is_authenticated = True
        self.is_anonymous = False


class FakeRequest:
    def __init__(self, user):
        self.user = user


class ShippedProvider(MandateScopeMixin):
    """Stand-in for a module's DefaultScopeProvider."""

    def can(self, request) -> bool:
        return self.mandate_admits(request)


class HostProvider(ShippedProvider):
    """A host subclass — still recognised as "the shipped one" by the check."""


class UnrelatedProvider:
    pass


@pytest.fixture
def provider():
    state = {"has_mandate": True, "raises": None}

    def handler(payload):
        if state["raises"]:
            raise state["raises"]
        return {MANDATE_RESULT_KEY: state["has_mandate"]}

    function_registry.register(MANDATE_FUNCTION, handler)
    yield state
    function_registry._providers.pop(MANDATE_FUNCTION, None)


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


def test_a_mandate_less_caller_is_refused(provider):
    """The whole point. Flip this to True and F1/F3/F4 are open again."""
    provider["has_mandate"] = False
    assert ShippedProvider().can(FakeRequest(FakeUser())) is False


def test_a_mandated_caller_is_admitted(provider):
    provider["has_mandate"] = True
    assert ShippedProvider().can(FakeRequest(FakeUser())) is True


def test_a_non_answer_refuses_with_503_not_403(provider):
    """"Could not ask" is not "you hold no mandate". The mutant that returns
    False here turns a workspaces blip into a verdict about the user."""
    provider["raises"] = RuntimeError("workspaces is down")
    with pytest.raises(MandateUnavailable) as exc:
        ShippedProvider().can(FakeRequest(FakeUser()))
    assert exc.value.status_code == 503


def test_a_standalone_deployment_keeps_single_tenant_semantics():
    """No seam, no workspaces: there are no mandates to hold, so refusing
    everyone would be a different bug. The check warns instead."""
    assert deployment_is_standalone() is True
    assert ShippedProvider().can(FakeRequest(FakeUser())) is True


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _check(provider_value):
    return check_shipped_scope_provider(
        setting="STAPEL_X['SCOPE_PROVIDER']",
        provider=provider_value,
        shipped_cls=ShippedProvider,
        error_id="stapel_x.E900",
        warning_id="stapel_x.W900",
        isolates="widget",
    )


def test_standalone_deployment_on_the_shipped_provider_warns():
    msgs = _check(ShippedProvider)
    assert [type(m) for m in msgs] == [checks.Warning]
    assert msgs[0].id == "stapel_x.W900"


def test_workspaces_present_and_still_shipped_is_an_error(provider):
    """The finding the old importability-and-type check could not make."""
    msgs = _check(ShippedProvider)
    assert [type(m) for m in msgs] == [checks.Error]
    assert msgs[0].id == "stapel_x.E900"


def test_a_host_subclass_of_the_shipped_provider_is_still_shipped(provider):
    """Subclassing the fail-open default does not launder it."""
    assert [m.id for m in _check(HostProvider())] == ["stapel_x.E900"]


def test_a_real_swap_is_silent(provider):
    assert _check(UnrelatedProvider()) == []


def test_the_error_survives_workspaces_being_installed_without_the_seam():
    """``deployment_is_standalone`` defers to the mandate module, which counts
    an in-process ``stapel_workspaces`` as answerable even with no route."""
    with override_settings(INSTALLED_APPS=[]):
        assert deployment_is_standalone() is True


# ---------------------------------------------------------------------------
# The library-view gate
# ---------------------------------------------------------------------------


class AnonSession:
    """An anonymous-axis account: authenticated, is_anonymous True."""

    pk = "anon-1"
    is_authenticated = True
    is_anonymous = True


def _gate(user):
    from stapel_core.django.api.permissions import HasWorkspaceMandateIfScoped

    return HasWorkspaceMandateIfScoped().has_permission(FakeRequest(user), None)


def test_scoped_gate_refuses_a_guest(provider):
    provider["has_mandate"] = False
    assert _gate(FakeUser()) is False


def test_scoped_gate_admits_a_mandated_account(provider):
    provider["has_mandate"] = True
    assert _gate(FakeUser()) is True


def test_scoped_gate_refuses_an_anonymous_session_in_every_shape(provider):
    assert _gate(AnonSession()) is False
    provider["has_mandate"] = True
    assert _gate(AnonSession()) is False


def test_scoped_gate_admits_in_a_standalone_deployment():
    """A single-tenant host mounting a library view keeps working; the strict
    class would answer 503 to everyone there."""
    assert _gate(FakeUser()) is True
    assert _gate(AnonSession()) is False


def test_scoped_gate_still_503s_when_a_wired_seam_cannot_answer(provider):
    """Unreachable-by-configuration is a deployment shape; unreachable-right-
    now is an outage. Only the first admits."""
    provider["raises"] = RuntimeError("workspaces is down")
    with pytest.raises(MandateUnavailable):
        _gate(FakeUser())
