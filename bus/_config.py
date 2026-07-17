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


class RedisStreamsBusConfig:
    """Redis Streams backend configuration.

    STAPEL_REDIS_BUS_URL          dedicated bus connection (falls back to
                                   REDIS_URL, the same instance django-redis
                                   already uses for cache/sessions — fine for
                                   a dev box; production should point this at
                                   its own Redis so a cache flush cannot also
                                   wipe consumer groups/pending entries)
    STAPEL_REDIS_BUS_CLAIM_IDLE_MS minimum idle time (ms) before a pending
                                   entry is considered abandoned and eligible
                                   for XAUTOCLAIM by another consumer
    STAPEL_REDIS_BUS_STREAM_MAXLEN approximate cap on stream length (XADD
                                   MAXLEN ~); 0 disables trimming

    A bus topic maps 1:1 to a stream key of the same name (no prefixing —
    mirrors the Kafka backend, where topic == Kafka topic verbatim).
    """

    @staticmethod
    def url() -> str:
        return _get("STAPEL_REDIS_BUS_URL", "") or _get("REDIS_URL", "redis://redis:6379/0")

    @staticmethod
    def claim_idle_ms() -> int:
        return int(_get("STAPEL_REDIS_BUS_CLAIM_IDLE_MS", "60000"))

    @staticmethod
    def maxlen() -> int:
        return int(_get("STAPEL_REDIS_BUS_STREAM_MAXLEN", "100000"))
