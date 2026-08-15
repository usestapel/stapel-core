"""The third principal state, and the refusal it degrades to.

Views across the fleet reason in two states — anonymous and authenticated —
and treat the second as sufficient. A registered account with no membership
anywhere is neither: it is stapel-workspaces' guest, a predicate that has
existed since the mandate-model vardict and had zero consumers outside its own
package, because in a split deployment no sibling could even ask (the
workspaces comm surface publishes only workspace-scoped questions).

What is pinned here: the three answers stay three, and the fourth outcome —
"could not ask" — never collapses into any of them.
"""
import pytest
from django.core import checks
from django.core.cache import cache
from django.test import override_settings

from stapel_core.comm import function_registry
from stapel_core.comm.exceptions import FunctionCallError
from stapel_core.django.api.permissions import (
    HasWorkspaceMandate,
    IsNotAnonymousUser,
    MandateUnavailable,
)
from stapel_core.django.mandate import (
    DEFAULT_MANDATE_CACHE_SECONDS,
    E001_MANDATE_SEAM_UNREACHABLE,
    MANDATE_CACHE_SETTING,
    MANDATE_FUNCTION,
    MANDATE_RESULT_KEY,
    MANDATE_REVOKING_ACTIONS,
    MandateLookupUnavailable,
    MandateState,
    _on_mandate_revoked,
    check_mandate_seam,
    has_mandate,
    invalidate_mandate_cache,
    mandate_cache_seconds,
    mandate_seam_unreachable_reason,
    mandate_state,
)


class FakeUser:
    def __init__(self, pk="u-1", authenticated=True):
        self.pk = pk
        self.is_authenticated = authenticated
        self.is_anonymous = not authenticated


class AnonUser:
    pk = None
    is_authenticated = False
    is_anonymous = True


class FakeRequest:
    def __init__(self, user):
        self.user = user


@pytest.fixture
def provider():
    """Register a mandate provider and hand back a switch for its answer."""
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
# Three states, three answers
# ---------------------------------------------------------------------------


def test_anonymous_is_its_own_answer(provider):
    assert mandate_state(AnonUser()) is MandateState.ANONYMOUS
    assert mandate_state(None) is MandateState.ANONYMOUS


def test_a_mandate_less_account_is_guest_not_anonymous(provider):
    """The whole point: a real account with no membership anywhere is a THIRD
    thing, and the vocabulary can now say so."""
    provider["has_mandate"] = False
    assert mandate_state(FakeUser()) is MandateState.GUEST


def test_a_mandated_account_is_mandated(provider):
    provider["has_mandate"] = True
    assert mandate_state(FakeUser()) is MandateState.MANDATED
    assert has_mandate(FakeUser()) is True


def test_the_three_states_are_pairwise_distinguishable(provider):
    provider["has_mandate"] = False
    guest = mandate_state(FakeUser("guest"))
    provider["has_mandate"] = True
    cache.clear()
    mandated = mandate_state(FakeUser("member"))
    anonymous = mandate_state(AnonUser())
    assert len({guest, mandated, anonymous}) == 3


# ---------------------------------------------------------------------------
# Fail closed, and say so
# ---------------------------------------------------------------------------


def test_a_failed_lookup_is_not_a_negative_answer(provider):
    """The mutant: return GUEST instead of raising in `_ask` and this dies.

    A failed lookup rendered as GUEST becomes a 403, which reads as a verdict
    about the user — the same mistake that once told a workspace's own owner
    they were Forbidden for weeks.
    """
    provider["raises"] = RuntimeError("workspaces is down")
    with pytest.raises(MandateLookupUnavailable):
        mandate_state(FakeUser())


def test_an_unwired_deployment_refuses_rather_than_admits():
    """No provider, no route, no stapel_workspaces installed."""
    assert mandate_seam_unreachable_reason() is not None
    with pytest.raises(MandateLookupUnavailable):
        mandate_state(FakeUser())


def test_a_provider_answering_without_the_key_is_a_non_answer(provider):
    def wrong(payload):
        return {"member": True}

    function_registry._providers[MANDATE_FUNCTION] = wrong
    with pytest.raises(MandateLookupUnavailable):
        mandate_state(FakeUser())


def test_a_non_answer_is_never_cached(provider):
    """A blip must not be remembered as a verdict for the next 30 seconds."""
    provider["raises"] = FunctionCallError("transient")
    with pytest.raises(MandateLookupUnavailable):
        mandate_state(FakeUser())
    provider["raises"] = None
    provider["has_mandate"] = True
    assert mandate_state(FakeUser()) is MandateState.MANDATED


# ---------------------------------------------------------------------------
# The permission class
# ---------------------------------------------------------------------------


def test_the_gate_admits_only_a_mandate_holder(provider):
    gate = HasWorkspaceMandate()
    provider["has_mandate"] = True
    assert gate.has_permission(FakeRequest(FakeUser("m")), None) is True
    cache.clear()
    provider["has_mandate"] = False
    assert gate.has_permission(FakeRequest(FakeUser("g")), None) is False
    assert gate.has_permission(FakeRequest(AnonUser()), None) is False


def test_the_gate_answers_503_when_it_cannot_ask(provider):
    """The mutant: catch MandateLookupUnavailable and `return False` — this
    assertion dies, and the deployment starts telling users they are
    Forbidden because a peer is down."""
    provider["raises"] = RuntimeError("down")
    with pytest.raises(MandateUnavailable) as raised:
        HasWorkspaceMandate().has_permission(FakeRequest(FakeUser()), None)
    assert raised.value.status_code == 503


def test_the_503_carries_the_registered_error_key():
    """The permission module spells the key out (api.errors imports it, so the
    arrow cannot point back); this is the gate that keeps the halves paired."""
    from stapel_core.django.api.errors import ERR_503_MANDATE_UNAVAILABLE

    assert MandateUnavailable.default_code == ERR_503_MANDATE_UNAVAILABLE


def test_the_new_gate_is_strictly_narrower_than_is_not_anonymous_user(provider):
    """`IsNotAnonymousUser` returning True for a guest is not a bug — it is
    what that class means. It is also why a calendar fix that reached for it
    did not close anything. The two must stay two."""
    provider["has_mandate"] = False
    guest = FakeRequest(FakeUser("guest"))
    assert IsNotAnonymousUser().has_permission(guest, None) is True
    assert HasWorkspaceMandate().has_permission(guest, None) is False


def test_the_gate_declares_its_anonymous_stance():
    from stapel_core.django.api.permissions import ANONYMOUS_DENIED

    assert HasWorkspaceMandate.stapel_anonymous_access == ANONYMOUS_DENIED


# ---------------------------------------------------------------------------
# The cache, and what invalidates it
# ---------------------------------------------------------------------------


def test_the_answer_is_cached(provider):
    calls = []
    original = function_registry._providers[MANDATE_FUNCTION]

    def counting(payload):
        calls.append(payload)
        return original(payload)

    function_registry._providers[MANDATE_FUNCTION] = counting
    user = FakeUser("cached")
    mandate_state(user)
    mandate_state(user)
    assert len(calls) == 1


def test_revocation_invalidates_immediately(provider):
    """A cache over an authorization answer with no invalidation is a security
    defect wearing a performance costume. The mutant: drop the subscription
    (or the user_id read) and the revoked mandate keeps opening doors."""
    user = FakeUser("revoked")
    provider["has_mandate"] = True
    assert mandate_state(user) is MandateState.MANDATED

    provider["has_mandate"] = False

    class Event:
        payload = {"workspace_id": "w-1", "user_id": "revoked"}

    _on_mandate_revoked(Event())
    assert mandate_state(user) is MandateState.GUEST


def test_the_revoking_actions_are_the_ones_that_take_a_mandate_away():
    """A role change moves a mandate, it never removes one — listing it here
    would buy nothing and cost an invalidation storm."""
    assert MANDATE_REVOKING_ACTIONS == (
        "workspace.member_removed",
        "workspace.member_suspended",
    )


def test_subscribing_wires_every_revoking_action():
    from stapel_core.comm import action_registry
    from stapel_core.django.mandate import subscribe_mandate_invalidation

    subscribe_mandate_invalidation()
    subscribe_mandate_invalidation()  # idempotent: ready() may run twice
    for action in MANDATE_REVOKING_ACTIONS:
        handlers = action_registry.handlers(action)
        assert [h for h in handlers if h is _on_mandate_revoked] == [
            _on_mandate_revoked
        ], action


def test_the_subscription_is_wired_by_the_app_not_by_each_product():
    """A cache with an invalidation nobody calls is a cache with none. The
    mutant: drop the call from CommonDjangoConfig.ready()."""
    import inspect

    from stapel_core.django import apps as apps_module

    source = inspect.getsource(apps_module)
    assert "subscribe_mandate_invalidation()" in source
    assert "_register_mandate_checks()" in source
    assert "_register_prodguard_checks()" in source


def test_invalidation_is_idempotent():
    invalidate_mandate_cache("never-seen")
    invalidate_mandate_cache("never-seen")


def test_the_ttl_can_be_switched_off(provider):
    calls = []
    original = function_registry._providers[MANDATE_FUNCTION]

    def counting(payload):
        calls.append(payload)
        return original(payload)

    function_registry._providers[MANDATE_FUNCTION] = counting
    user = FakeUser("uncached")
    with override_settings(**{MANDATE_CACHE_SETTING: 0}):
        mandate_state(user)
        mandate_state(user)
    assert len(calls) == 2


def test_an_unreadable_ttl_means_the_default_not_no_cache():
    """A typo must not silently disable the invalidation path either way."""
    with override_settings(**{MANDATE_CACHE_SETTING: "soon"}):
        assert mandate_cache_seconds() == DEFAULT_MANDATE_CACHE_SECONDS
    with override_settings(**{MANDATE_CACHE_SETTING: -5}):
        assert mandate_cache_seconds() == 0


# ---------------------------------------------------------------------------
# The boot-time half: an unwired seam is loud
# ---------------------------------------------------------------------------


def test_the_seam_check_is_quiet_when_no_view_needs_a_mandate():
    """An unconditional error is how a whole tag ends up silenced."""
    assert mandate_seam_unreachable_reason() is not None
    assert check_mandate_seam() == []


def test_the_seam_check_refuses_a_deployment_that_cannot_ask(monkeypatch):
    """The mutant: make `mandate_seam_unreachable_reason` return None on an
    unwired deployment and this Error disappears — leaving a service whose
    mandated views all answer 503, discovered by the first user."""
    monkeypatch.setattr(
        "stapel_core.django.mandate._views_requiring_a_mandate",
        lambda: ["app.views.TaskListView"],
    )
    findings = check_mandate_seam()
    assert [f.id for f in findings] == [E001_MANDATE_SEAM_UNREACHABLE]
    assert findings[0].level >= checks.ERROR
    assert "app.views.TaskListView" in findings[0].msg


def test_the_seam_finding_cannot_be_muted_by_a_blanket_line(monkeypatch):
    monkeypatch.setattr(
        "stapel_core.django.mandate._views_requiring_a_mandate",
        lambda: ["app.views.TaskListView"],
    )
    finding = check_mandate_seam()[0]
    with override_settings(SILENCED_SYSTEM_CHECKS=[E001_MANDATE_SEAM_UNREACHABLE]):
        assert finding.is_silenced() is False


def test_a_wired_provider_makes_the_seam_reachable(provider):
    assert mandate_seam_unreachable_reason() is None
