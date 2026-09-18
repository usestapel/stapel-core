"""A library that registers a GDPRProvider has already said what it owns.

THE FAILURE
-----------
A classified fleet's svc-classified-core installs stapel-listings,
stapel-reviews and stapel-moderation. All three register a `GDPRProvider` in
`gdpr_registry` from their `AppConfig.ready()`. The host declares all three in
`STAPEL_GDPR["DATA_OWNERS"]`. Every part of that looks done.

None of them answers an erasure. `gdpr_registry` is the MONOLITH path — the
orchestrator walks `gdpr_registry.providers` in its own process. In a split
fleet the provider sits in a peer, and the only thing that reaches it is
`gdpr.erasure.requested`, which requires a SECOND, separate act of
registration (`register_gdpr_owner`) that those libraries never perform.
stapel-agent does perform it, which is why `agent` answered and `listings`,
`reviews` and `moderation` did not.

Measured on a live fleet, 2026-09-17, on erasure request id 1 — the first ever
run there. Ten declared owners, six able to answer, four parts stuck `pending`
forever. `completeness_waived=False`, so the request can never reach `deleted`.
Nobody had found out, because finding out requires running an erasure.

WHY THE BRIDGE AND NOT A DOCS LINE
----------------------------------
Requiring a library to remember a second registration makes correctness
something each consumer opts into, and the failure is silent by construction:
every part reports success except the ones that report nothing at all. A
provider IS a declaration of ownership. Being registered is the act.

The bridge is at DISPATCH time, not at ready() time, and that is deliberate:
`AppConfig.ready()` order is the host's INSTALLED_APPS order, so a bridge that
registered eagerly would race any library that wires itself explicitly and
would raise `one name is one owner` depending on list position. At dispatch the
question "does this section already have an explicit owner?" has a settled
answer.
"""
import pytest

pytestmark = pytest.mark.django_db

ERASURE = "gdpr.erasure.requested"
PROBE = "gdpr.owner.probe"
SECTION_ERASED = "gdpr.section.erased"
OWNER_ALIVE = "gdpr.owner.alive"
INPROCESS = {"OUTBOX_ENABLED": False, "ACTION_TRANSPORT": "inprocess"}


def _hand_written_erasure_handler(event):
    """The sixty lines eight libraries carry, reduced to what matters here.

    Defined in this module on purpose: attribution charges a handler to the
    app that defines it, and the provider below is defined here too — which
    is exactly the shape of a library that registers both.
    """
    from stapel_core.comm import emit

    payload = event.payload
    emit(SECTION_ERASED, {
        "owner": "recordings",
        "subject_type": payload["subject_type"],
        "subject_key": payload["subject_key"],
        "correlation_id": payload["correlation_id"],
        "counts": {"recordings": 1},
    }, key=payload["subject_key"])


class _Provider:
    def __init__(self, section):
        self.section = section
        self.anonymized = []
        self.deleted = []

    def export(self, user_id):
        return {}

    def delete(self, user_id):
        self.deleted.append(user_id)

    def anonymize(self, user_id):
        self.anonymized.append(user_id)


class _Event:
    def __init__(self, payload):
        self.payload = payload
        self.event_id = "e1"


def _request(subject_key="u1", correlation_id="c1", subject_type="account"):
    return _Event({
        "correlation_id": correlation_id,
        "subject_type": subject_type,
        "subject_key": subject_key,
    })


@pytest.fixture
def registry(monkeypatch):
    from stapel_core.gdpr import GDPRRegistry

    fresh = GDPRRegistry()
    monkeypatch.setattr("stapel_core.gdpr.gdpr_registry", fresh)
    monkeypatch.setattr("stapel_core.gdpr.provider_bridge.gdpr_registry", fresh)
    from stapel_core.gdpr import provider_bridge

    provider_bridge._reset_bridge()
    yield fresh
    provider_bridge._reset_bridge()


class _Subscriptions:
    """Subscribe hand-written handlers the way ``@on_action`` does."""

    def __init__(self):
        self.handlers = []

    def hand_written(self, action):
        from stapel_core.comm.registry import action_registry

        calls = []

        def handle(event):
            calls.append(event.payload.get("subject_key"))

        action_registry.subscribe(action, handle)
        self.handlers.append(handle)
        return calls


@pytest.fixture
def subscriptions():
    """Snapshot the gdpr subscriptions and put them back afterwards."""
    from stapel_core.comm.registry import action_registry

    names = (ERASURE, PROBE, SECTION_ERASED, OWNER_ALIVE)
    before = {name: action_registry.handlers(name) for name in names}
    for name in names:
        # Earlier tests in this file register owners, and register_gdpr_owner
        # subscribes for the whole process; start from an empty fan-out.
        action_registry._subscribers[name] = []
    try:
        yield _Subscriptions()
    finally:
        # No unsubscribe in the registry: restore the lists this test found.
        for name, handlers in before.items():
            action_registry._subscribers[name] = list(handlers)


@pytest.fixture
def receipts(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "stapel_core.gdpr.owners._emit_receipt",
        lambda owner, corr, st, sk, counts: seen.append(
            {"owner": owner, "correlation_id": corr, "subject_type": st,
             "subject_key": sk, "counts": dict(counts)}),
    )
    return seen


class TestARegisteredProviderAnswers:
    def test_it_erases_and_receipts(self, registry, receipts):
        from stapel_core.gdpr import provider_bridge

        p = _Provider("listings")
        registry.register(p)

        provider_bridge.bridge_erasure_requested(_request())

        assert p.anonymized == ["u1"], "monolith order: anonymize, then delete"
        assert p.deleted == ["u1"]
        assert [r["owner"] for r in receipts] == ["listings"]

    def test_every_registered_provider_answers(self, registry, receipts):
        from stapel_core.gdpr import provider_bridge

        for name in ("listings", "reviews", "moderation"):
            registry.register(_Provider(name))

        provider_bridge.bridge_erasure_requested(_request())

        assert sorted(r["owner"] for r in receipts) == [
            "listings", "moderation", "reviews"
        ], "the four-pending-parts failure, as a test"

    def test_it_answers_the_probe(self, registry):
        from stapel_core.gdpr import provider_bridge

        registry.register(_Provider("reviews"))
        emitted = []
        import stapel_core.comm as comm

        real = comm.emit
        try:
            comm.emit = lambda name, payload, **kw: emitted.append((name, payload))
            provider_bridge.bridge_owner_probe(
                _Event({"correlation_id": "c1"})
            )
        finally:
            comm.emit = real
        assert [n for n, _ in emitted] == ["gdpr.owner.alive"]
        assert emitted[0][1]["owner"] == "reviews"

    def test_it_claims_account_only(self, registry, receipts):
        """A provider's interface is delete(user_id). It has no other key."""
        from stapel_core.gdpr import provider_bridge

        p = _Provider("listings")
        registry.register(p)

        provider_bridge.bridge_erasure_requested(_request(subject_type="workspace"))

        assert p.deleted == []
        assert receipts == [], "claiming a type it cannot erase would be a lie"


class TestExplicitWiringStillWins:
    def test_a_section_with_its_own_owner_is_not_bridged(self, registry, receipts):
        """stapel-agent registers BOTH. It must not answer twice."""
        from stapel_core.gdpr import provider_bridge
        from stapel_core.gdpr.owners import _reset_gdpr_owners, register_gdpr_owner

        _reset_gdpr_owners()
        try:
            p = _Provider("agent")
            registry.register(p)
            register_gdpr_owner("agent", ["account"], lambda t, k, w=None: {"own": 1})

            provider_bridge.bridge_erasure_requested(_request())

            assert p.deleted == [], "the library's own erase is authoritative"
            assert receipts == [], "and its own handler emits the one receipt"
        finally:
            _reset_gdpr_owners()

    def test_the_bridge_sees_registration_order_independently(self, registry, receipts):
        """Registered after the bridge's subscriber exists — still skipped.

        This is why the bridge dispatches rather than registers: ready() order
        is the host's INSTALLED_APPS order and cannot be relied on.
        """
        from stapel_core.gdpr import provider_bridge
        from stapel_core.gdpr.owners import _reset_gdpr_owners, register_gdpr_owner

        _reset_gdpr_owners()
        try:
            p = _Provider("agent")
            registry.register(p)
            provider_bridge.bridge_erasure_requested(_request(correlation_id="c0"))
            assert p.deleted == ["u1"], "no explicit owner yet — bridge answers"

            register_gdpr_owner("agent", ["account"], lambda t, k, w=None: {"own": 1})
            provider_bridge.bridge_erasure_requested(_request(correlation_id="c1"))
            assert p.deleted == ["u1"], "explicit owner now exists — bridge stands down"
        finally:
            _reset_gdpr_owners()


class TestADeclaredOwnerWithNoAnswerer:
    """gdpr.E011 — loud at boot, not discovered by the first erasure."""

    def test_a_provider_section_with_no_subscriber_is_an_error(
        self, registry, monkeypatch
    ):
        from stapel_core.gdpr import provider_bridge

        registry.register(_Provider("listings"))
        monkeypatch.setattr(provider_bridge, "_bridge_is_subscribed", lambda: False)

        problems = provider_bridge.check_owners_are_answerable()
        assert [p.id for p in problems] == [provider_bridge.E011]
        assert "listings" in problems[0].msg

    def test_a_bridged_section_is_clean(self, registry, monkeypatch):
        from stapel_core.gdpr import provider_bridge

        registry.register(_Provider("listings"))
        monkeypatch.setattr(provider_bridge, "_bridge_is_subscribed", lambda: True)

        assert provider_bridge.check_owners_are_answerable() == []

    def test_an_explicitly_wired_section_is_clean(self, registry, monkeypatch):
        from stapel_core.gdpr import provider_bridge
        from stapel_core.gdpr.owners import _reset_gdpr_owners, register_gdpr_owner

        _reset_gdpr_owners()
        try:
            registry.register(_Provider("agent"))
            register_gdpr_owner("agent", ["account"], lambda t, k, w=None: {})
            monkeypatch.setattr(provider_bridge, "_bridge_is_subscribed", lambda: False)

            assert provider_bridge.check_owners_are_answerable() == []
        finally:
            _reset_gdpr_owners()


class TestALibraryThatAnswersForItselfIsNotBridged:
    """The bridge must yield to a hand-written ``@on_action`` handler.

    0.83.0 skipped a section only when it had called ``register_gdpr_owner``.
    Eight libraries in this fleet register a ``GDPRProvider`` AND hand-write
    the sixty lines of the protocol with ``@on_action`` — they never call
    ``register_gdpr_owner``, so the bridge answered beside them and one
    erasure minted two receipts for one part. A receipt says a deletion
    happened; two of them say it happened twice, which is a false legal
    record.
    """

    def test_one_part_gets_one_receipt(self, registry, subscriptions):
        from django.test import override_settings

        from stapel_core.bus.event import Event
        from stapel_core.comm.actions import deliver
        from stapel_core.comm.registry import action_registry
        from stapel_core.gdpr import provider_bridge

        provider = _Provider("recordings")
        registry.register(provider)

        receipted = []
        action_registry.subscribe(
            SECTION_ERASED, lambda e: receipted.append(e.payload["owner"])
        )
        action_registry.subscribe(ERASURE, _hand_written_erasure_handler)
        action_registry.subscribe(ERASURE, provider_bridge.bridge_erasure_requested)

        with override_settings(STAPEL_COMM=INPROCESS):
            deliver(Event(
                event_type=ERASURE,
                service="gdpr",
                payload={
                    "correlation_id": "c1",
                    "subject_type": "account",
                    "subject_key": "u1",
                },
            ))

        assert receipted == ["recordings"], "one part, one receipt"
        assert provider.deleted == [], "the library erases through its own path"

    def test_the_bridge_skips_it(self, registry, receipts, subscriptions):
        from stapel_core.gdpr import provider_bridge

        provider = _Provider("recordings")
        registry.register(provider)
        subscriptions.hand_written(ERASURE)

        provider_bridge.bridge_erasure_requested(_request())

        assert provider.deleted == [], "the library's own handler is the owner"
        assert receipts == []

    def test_the_probe_is_skipped_for_the_same_reason(self, registry, subscriptions):
        from stapel_core.comm.registry import action_registry
        from stapel_core.gdpr import provider_bridge

        registry.register(_Provider("recordings"))
        subscriptions.hand_written(PROBE)

        alive = []
        action_registry.subscribe(OWNER_ALIVE, lambda e: alive.append(e.payload))
        import stapel_core.comm as comm

        real = comm.emit
        try:
            comm.emit = lambda name, payload, **kw: alive.append((name, payload))
            provider_bridge.bridge_owner_probe(_Event({"correlation_id": "c1"}))
        finally:
            comm.emit = real
        assert alive == [], "two owner.alive answers for one owner is two owners"

    def test_a_provider_with_no_hand_handler_is_still_bridged(
        self, registry, receipts, subscriptions
    ):
        """Only the hand-handled app stands the bridge down."""
        from stapel_core.gdpr import provider_bridge

        registry.register(_Provider("listings"))

        provider_bridge.bridge_erasure_requested(_request())

        assert [r["owner"] for r in receipts] == ["listings"]
        assert provider_bridge.check_bridge_yields_to_hand_handlers() == []

    def test_a_hand_handler_without_a_provider_changes_nothing(
        self, registry, receipts, subscriptions
    ):
        from stapel_core.gdpr import provider_bridge

        subscriptions.hand_written(ERASURE)

        provider_bridge.bridge_erasure_requested(_request())

        assert receipts == []
        assert provider_bridge.check_bridge_yields_to_hand_handlers() == []


class TestBridgedAndHandHandledIsAWarning:
    """gdpr.W012 — the bridge yields, and says whose copy to delete."""

    def test_it_names_the_section_and_the_hand_written_module(
        self, registry, subscriptions
    ):
        from stapel_core.gdpr import provider_bridge

        registry.register(_Provider("recordings"))
        subscriptions.hand_written(ERASURE)

        problems = provider_bridge.check_bridge_yields_to_hand_handlers()

        assert [p.id for p in problems] == [provider_bridge.W012]
        assert "recordings" in problems[0].msg
        assert _hand_written_erasure_handler.__module__ in problems[0].msg
        assert problems[0].__class__.__name__ == "Warning", "never blocks a boot"

    def test_an_explicitly_registered_owner_is_not_warned_about(
        self, registry, subscriptions
    ):
        from stapel_core.gdpr import provider_bridge
        from stapel_core.gdpr.owners import _reset_gdpr_owners, register_gdpr_owner

        _reset_gdpr_owners()
        try:
            registry.register(_Provider("agent"))
            register_gdpr_owner("agent", ["account"], lambda t, k, w=None: {})
            assert provider_bridge.check_bridge_yields_to_hand_handlers() == []
        finally:
            _reset_gdpr_owners()
