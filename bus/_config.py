"""
Connection settings for bus backends.

Resolution order for every value: environment variable first, then the
Django setting of the same name, then the default — a deployment switches
transports and endpoints purely through the environment (12-factor), while
tests keep configuring Django settings.
"""
from __future__ import annotations

import os


def _get(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value:
        return value
    try:
        from django.conf import settings

        return getattr(settings, name, default) or default
    except Exception:  # settings not configured (plain scripts)
        return default


class KafkaBusConfig:
    @staticmethod
    def _base() -> dict:
        cfg: dict = {
            "bootstrap.servers": _get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
        }
        protocol = _get("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
        if protocol != "PLAINTEXT":
            cfg["security.protocol"] = protocol
        mechanism = _get("KAFKA_SASL_MECHANISM", "")
        if mechanism:
            cfg["sasl.mechanism"] = mechanism
            cfg["sasl.username"] = _get("KAFKA_SASL_USERNAME", "")
            cfg["sasl.password"] = _get("KAFKA_SASL_PASSWORD", "")
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


class NatsBusConfig:
    """JetStream backend configuration.

    NATS_URL                 broker address        (nats://nats:4222)
    STAPEL_NATS_STREAM       JetStream stream name (stapel-events)
    STAPEL_NATS_EVENT_PREFIX subject prefix        (stapel.evt)

    Every bus topic maps to the subject ``<prefix>.<topic>``; the stream
    captures ``<prefix>.>`` so new topics need no broker-side changes.
    """

    @staticmethod
    def url() -> str:
        return _get("NATS_URL", "nats://nats:4222")

    @staticmethod
    def stream() -> str:
        return _get("STAPEL_NATS_STREAM", "stapel-events")

    @staticmethod
    def subject_prefix() -> str:
        return _get("STAPEL_NATS_EVENT_PREFIX", "stapel.evt")

    @classmethod
    def subject_for(cls, topic: str) -> str:
        return f"{cls.subject_prefix()}.{topic}"
