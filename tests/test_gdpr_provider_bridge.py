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
