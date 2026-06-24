"""
Kafka health check utility.
"""

import logging

from .config import KafkaConfig

logger = logging.getLogger(__name__)


def check_kafka_health(timeout: float = 5.0) -> dict:
    """
    Check if Kafka is reachable.

    Returns:
        dict with 'healthy' (bool) and 'detail' (str)
    """
    config = KafkaConfig.from_settings()
    if not config.is_configured:
        return {"healthy": False, "detail": "Kafka not configured"}

    try:
        from confluent_kafka.admin import AdminClient

        admin = AdminClient(config.to_confluent_config())
        metadata = admin.list_topics(timeout=timeout)
        broker_count = len(metadata.brokers)
        topic_count = len(metadata.topics)
        return {
            "healthy": broker_count > 0,
            "detail": f"{broker_count} broker(s), {topic_count} topic(s)",
        }
    except Exception as e:
        logger.error("Kafka health check failed: %s", e)
        return {"healthy": False, "detail": str(e)}
