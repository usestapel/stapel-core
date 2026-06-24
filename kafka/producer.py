"""
Thread-safe singleton Kafka producer.
"""

import logging
import threading

from .config import KafkaConfig
from .events import Event

logger = logging.getLogger(__name__)

_producer = None
_producer_lock = threading.Lock()


def _delivery_callback(err, msg):
    """Callback for Kafka delivery reports."""
    if err:
        logger.error("Kafka delivery failed: %s (topic=%s)", err, msg.topic())
    else:
        logger.debug(
            "Kafka message delivered: topic=%s partition=%s offset=%s",
            msg.topic(), msg.partition(), msg.offset(),
        )


def _get_producer():
    """Get or create the singleton producer instance."""
    global _producer
    if _producer is not None:
        return _producer

    with _producer_lock:
        if _producer is not None:
            return _producer

        from confluent_kafka import Producer

        config = KafkaConfig.from_settings()
        if not config.is_configured:
            logger.warning("Kafka is not configured, producer will be unavailable")
            return None

        conf = config.to_confluent_config()
        conf["linger.ms"] = 5  # Small batch window for low latency
        conf["acks"] = "all"
        _producer = Producer(conf)
        logger.info("Kafka producer initialized: %s", config.bootstrap_servers)
        return _producer


def publish_event(topic: str, event: Event, key: str | None = None) -> bool:
    """
    Publish an event to a Kafka topic.

    Fire-and-forget with delivery callback for logging.
    Returns True if the event was queued, False if Kafka is unavailable.

    Args:
        topic: Kafka topic name
        event: Event envelope to publish
        key: Optional partition key for ordering (e.g. str(ad_id))
    """
    producer = _get_producer()
    if producer is None:
        logger.warning("Kafka producer unavailable, event not published: %s", event.event_type)
        return False

    try:
        producer.produce(
            topic=topic,
            value=event.to_bytes(),
            key=key.encode("utf-8") if key else None,
            callback=_delivery_callback,
        )
        # Trigger delivery callbacks without blocking
        producer.poll(0)
        return True
    except BufferError:
        logger.error("Kafka producer buffer full, flushing and retrying")
        producer.flush(timeout=5)
        try:
            producer.produce(
                topic=topic,
                value=event.to_bytes(),
                key=key.encode("utf-8") if key else None,
                callback=_delivery_callback,
            )
            producer.poll(0)
            return True
        except Exception as e:
            logger.error("Kafka publish failed after flush: %s", e)
            return False
    except Exception as e:
        logger.error("Kafka publish failed: %s", e)
        return False


def flush(timeout: float = 10):
    """Flush all pending messages. Call on shutdown."""
    if _producer is not None:
        remaining = _producer.flush(timeout=timeout)
        if remaining > 0:
            logger.warning("Kafka flush timed out, %d messages remaining", remaining)
