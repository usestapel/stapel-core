"""Tests for stapel_core.bus._config — KafkaBusConfig settings parsing."""
from django.test import override_settings

from stapel_core.bus._config import KafkaBusConfig


class TestBaseConfig:
    def test_defaults_just_bootstrap(self):
        with override_settings(KAFKA_BOOTSTRAP_SERVERS="broker:9092"):
            cfg = KafkaBusConfig._base()
        assert cfg == {"bootstrap.servers": "broker:9092"}

    def test_sasl_added_when_mechanism_set(self):
        with override_settings(
            KAFKA_BOOTSTRAP_SERVERS="broker:9092",
            KAFKA_SECURITY_PROTOCOL="SASL_SSL",
            KAFKA_SASL_MECHANISM="PLAIN",
            KAFKA_SASL_USERNAME="u",
            KAFKA_SASL_PASSWORD="p",
        ):
            cfg = KafkaBusConfig._base()
        assert cfg["security.protocol"] == "SASL_SSL"
        assert cfg["sasl.mechanism"] == "PLAIN"
        assert cfg["sasl.username"] == "u"
        assert cfg["sasl.password"] == "p"

    def test_sasl_skipped_when_mechanism_empty(self):
        with override_settings(
            KAFKA_BOOTSTRAP_SERVERS="broker:9092",
            KAFKA_SECURITY_PROTOCOL="SSL",
            KAFKA_SASL_MECHANISM="",
        ):
            cfg = KafkaBusConfig._base()
        assert cfg == {"bootstrap.servers": "broker:9092", "security.protocol": "SSL"}


class TestProducerConsumerConfig:
    def test_producer_adds_acks(self):
        with override_settings(KAFKA_BOOTSTRAP_SERVERS="b:9092"):
            cfg = KafkaBusConfig.producer_config()
        assert cfg["acks"] == "all"
        assert cfg["bootstrap.servers"] == "b:9092"

    def test_consumer_adds_group_and_offsets(self):
        with override_settings(KAFKA_BOOTSTRAP_SERVERS="b:9092"):
            cfg = KafkaBusConfig.consumer_config("svc-group")
        assert cfg["group.id"] == "svc-group"
        assert cfg["auto.offset.reset"] == "earliest"
        assert cfg["enable.auto.commit"] is False
