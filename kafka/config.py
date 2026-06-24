"""
Kafka configuration from Django settings / environment variables.
"""

import os
from dataclasses import dataclass


@dataclass
class KafkaConfig:
    """Kafka connection configuration."""

    bootstrap_servers: str = ""
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str = ""
    sasl_username: str = ""
    sasl_password: str = ""

    @classmethod
    def from_settings(cls) -> "KafkaConfig":
        """Load config from Django settings (falls back to env vars)."""
        try:
            from django.conf import settings
            return cls(
                bootstrap_servers=getattr(settings, "KAFKA_BOOTSTRAP_SERVERS", "") or os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
                security_protocol=getattr(settings, "KAFKA_SECURITY_PROTOCOL", "") or os.getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT"),
                sasl_mechanism=getattr(settings, "KAFKA_SASL_MECHANISM", "") or os.getenv("KAFKA_SASL_MECHANISM", ""),
                sasl_username=getattr(settings, "KAFKA_SASL_USERNAME", "") or os.getenv("KAFKA_CLIENT_USER", ""),
                sasl_password=getattr(settings, "KAFKA_SASL_PASSWORD", "") or os.getenv("KAFKA_CLIENT_PASSWORD", ""),
            )
        except Exception:
            return cls.from_env()

    @classmethod
    def from_env(cls) -> "KafkaConfig":
        """Load config directly from environment variables."""
        return cls(
            bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"),
            security_protocol=os.getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT"),
            sasl_mechanism=os.getenv("KAFKA_SASL_MECHANISM", ""),
            sasl_username=os.getenv("KAFKA_CLIENT_USER", ""),
            sasl_password=os.getenv("KAFKA_CLIENT_PASSWORD", ""),
        )

    def to_confluent_config(self) -> dict:
        """Convert to confluent-kafka configuration dict."""
        conf = {
            "bootstrap.servers": self.bootstrap_servers,
        }
        if self.security_protocol and self.security_protocol != "PLAINTEXT":
            conf["security.protocol"] = self.security_protocol
        if self.sasl_mechanism:
            conf["sasl.mechanism"] = self.sasl_mechanism
        if self.sasl_username:
            conf["sasl.username"] = self.sasl_username
        if self.sasl_password:
            conf["sasl.password"] = self.sasl_password
        return conf

    @property
    def is_configured(self) -> bool:
        """Check if Kafka is configured (has bootstrap servers)."""
        return bool(self.bootstrap_servers)
