"""A Kafka consumer provisions the topics it subscribes to.

Owner-reported, 2026-07-26, on the ironmemo stand:

    ERROR stapel_core.bus.backends.kafka: KafkaBus consumer error:
    KafkaError{code=UNKNOWN_TOPIC_OR_PART,...,str="Subscribed topic not
    available: recording.completed: ..."}

— six recordings topics that the consumer subscribes to, and that nobody had
added to the deploy script's hand-maintained `kafka-topics.sh --create` list.
The container stayed "Up", nothing was ever delivered, and the only symptom
was ERROR spam. The consumer already declares its topics on the line above
`subscribe()`; that declaration is now the single source of truth.
"""
import sys
import types

import pytest
from django.test import override_settings

from stapel_core.bus._config import KafkaBusConfig
from stapel_core.bus.backends.kafka import KafkaBus


class FakeFuture:
    def __init__(self, error=None):
        self._error = error

    def result(self):
        if self._error:
            raise self._error


class FakeAdminClient:
    """Stands in for confluent_kafka.admin.AdminClient."""

    instances: list["FakeAdminClient"] = []

    def __init__(self, config):
        self.config = config
        self.created: list[str] = []
        FakeAdminClient.instances.append(self)

    def list_topics(self, timeout=None):
        return types.SimpleNamespace(topics=dict.fromkeys(FakeAdminClient.existing))

    def create_topics(self, new_topics):
        self.created = [t.name for t in new_topics]
        return {t.name: FakeFuture(FakeAdminClient.create_error) for t in new_topics}


class FakeNewTopic:
    def __init__(self, name, num_partitions=1, replication_factor=1):
        self.name = name
        self.num_partitions = num_partitions
        self.replication_factor = replication_factor


@pytest.fixture
def admin(monkeypatch):
    FakeAdminClient.instances = []
    FakeAdminClient.existing = []
    FakeAdminClient.create_error = None
    module = types.ModuleType("confluent_kafka.admin")
    module.AdminClient = FakeAdminClient
    module.NewTopic = FakeNewTopic
    package = types.ModuleType("confluent_kafka")
    package.admin = module
    monkeypatch.setitem(sys.modules, "confluent_kafka", package)
    monkeypatch.setitem(sys.modules, "confluent_kafka.admin", module)
    return FakeAdminClient


class TestProvisioning:
    def test_creates_the_topics_that_are_missing(self, admin):
        admin.existing = ["recording.uploaded", "recording.uploaded.dlq"]
        KafkaBus()._provision_topics(["recording.uploaded", "recording.completed"])
        assert admin.instances[0].created == [
            "recording.completed",
            "recording.completed.dlq",
        ]

    def test_creates_the_dlq_alongside_each_topic(self, admin):
        """A poison message with no DLQ to park it in is a dropped message."""
        KafkaBus()._provision_topics(["a.topic"])
        assert admin.instances[0].created == ["a.topic", "a.topic.dlq"]

    def test_no_admin_call_at_all_when_everything_exists(self, admin):
        admin.existing = ["a.topic", "a.topic.dlq"]
        KafkaBus()._provision_topics(["a.topic", "a.topic"])
        assert admin.instances[0].created == []

    def test_honours_partition_and_replication_settings(self, admin):
        captured = {}

        class Capturing(FakeAdminClient):
            def create_topics(self, new_topics):
                captured["topics"] = new_topics
                return super().create_topics(new_topics)

        sys.modules["confluent_kafka.admin"].AdminClient = Capturing
        with override_settings(
            KAFKA_TOPIC_PARTITIONS="3", KAFKA_TOPIC_REPLICATION_FACTOR="2"
        ):
            KafkaBus()._provision_topics(["a.topic"])
        assert captured["topics"][0].num_partitions == 3
        assert captured["topics"][0].replication_factor == 2

    def test_opt_out_leaves_the_broker_alone(self, admin):
        """For deployments where topics are infra-owned and apps hold no ACL."""
        with override_settings(KAFKA_PROVISION_TOPICS="false"):
            KafkaBus()._provision_topics(["a.topic"])
        assert admin.instances == []

    def test_a_refused_creation_is_not_fatal(self, admin):
        """No create ACL, or a race with another consumer — the topics may
        very well already be there, and a consumer that refuses to start over
        this would be strictly worse than one that tries to subscribe."""
        admin.create_error = RuntimeError("TOPIC_AUTHORIZATION_FAILED")
        KafkaBus()._provision_topics(["a.topic"])  # must not raise

    def test_a_broker_that_cannot_be_reached_is_not_fatal(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("no route to broker")

        module = types.ModuleType("confluent_kafka.admin")
        module.AdminClient = boom
        module.NewTopic = FakeNewTopic
        package = types.ModuleType("confluent_kafka")
        package.admin = module
        monkeypatch.setitem(sys.modules, "confluent_kafka", package)
        monkeypatch.setitem(sys.modules, "confluent_kafka.admin", module)
        KafkaBus()._provision_topics(["a.topic"])  # must not raise


class TestUnknownTopicLogging:
    def test_logged_once_per_topic_not_once_per_poll(self, caplog):
        """librdkafka re-reports this on every metadata refresh — several
        lines per second, per topic. At ERROR it buried every real failure."""
        bus = KafkaBus()
        with caplog.at_level("WARNING"):
            for _ in range(50):
                bus._log_once_per_topic("Subscribed topic not available: a.topic")
                bus._log_once_per_topic("Subscribed topic not available: b.topic")
        assert len(caplog.records) == 2
        assert all(r.levelname == "WARNING" for r in caplog.records)


class TestProvisionSetting:
    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
    def test_falsey_values_disable(self, value):
        with override_settings(KAFKA_PROVISION_TOPICS=value):
            assert KafkaBusConfig.provision_topics() is False

    def test_on_by_default(self):
        assert KafkaBusConfig.provision_topics() is True
