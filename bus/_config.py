"""
Reads Kafka connection settings from Django settings.
"""
from __future__ import annotations

from django.conf import settings


class KafkaBusConfig:
    @staticmethod
    def _base() -> dict:
        cfg: dict = {
            "bootstrap.servers": getattr(settings, "KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
        }
        protocol = getattr(settings, "KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
        if protocol != "PLAINTEXT":
            cfg["security.protocol"] = protocol
        mechanism = getattr(settings, "KAFKA_SASL_MECHANISM", "")
        if mechanism:
            cfg["sasl.mechanism"] = mechanism
            cfg["sasl.username"] = getattr(settings, "KAFKA_SASL_USERNAME", "")
            cfg["sasl.password"] = getattr(settings, "KAFKA_SASL_PASSWORD", "")
        return cfg

    @classmethod
    def producer_config(cls) -> dict:
        return {**cls._base(), "acks": "all"}

    @classmethod
    def consumer_config(cls, group: str) -> dict:
        return {
            **cls._base(),
            "group.id": group,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
