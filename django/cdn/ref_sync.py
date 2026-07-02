"""
CDN reference sync utility.

Publishes ref-sync events to the message bus so CDN can update its ref table
asynchronously. If CDN is temporarily down, events accumulate in Kafka and
CDN catches up when it restarts.

For synchronous existence checks (read-only) use check_cdn_media_exists().
"""

import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)

TOPIC_CDN_REF_SYNC = "stapel.cdn.ref-sync"  # default; override via setting


def get_ref_sync_topic() -> str:
    from django.conf import settings

    return getattr(settings, "STAPEL_TOPIC_CDN_REF_SYNC", TOPIC_CDN_REF_SYNC)


@dataclass
class RefSyncResult:
    """Result of a CDN ref sync publish call."""

    ok: bool = True  # False if the bus publish failed
    errors: List[str] = field(default_factory=list)


def sync_cdn_refs(service, entity_type, entity_id, old_refs, new_refs) -> RefSyncResult:
    """
    Publish a ref-sync event to the CDN topic.

    CDN consumes this event asynchronously and updates its refs table.
    Returns RefSyncResult(ok=True) on successful publish, ok=False on error.

    Args:
        service: Service name (e.g. 'profiles')
        entity_type: Entity type (e.g. 'ad', 'ad_draft', 'review', 'profile')
        entity_id: Entity identifier (string or int)
        old_refs: Previous media references (e.g. ['product/hash1'])
        new_refs: Current media references (e.g. ['product/hash2'])
    """
    from stapel_core.bus import Event, publish

    old_set = set(old_refs or [])
    new_set = set(new_refs or [])

    if old_set == new_set:
        return RefSyncResult()

    try:
        publish(
            get_ref_sync_topic(),
            Event(
                event_type="cdn.ref.sync",
                service=service,
                payload={
                    "service": service,
                    "entity_type": entity_type,
                    "entity_id": str(entity_id),
                    "old_hashes": list(old_set),
                    "new_hashes": list(new_set),
                },
                key=str(entity_id),
            ),
        )
        return RefSyncResult(ok=True)
    except Exception as e:
        logger.warning(
            "CDN ref sync publish failed: %s service=%s entity=%s/%s",
            e,
            service,
            entity_type,
            entity_id,
        )
        return RefSyncResult(ok=False)


def check_cdn_media_exists(ref_str: str) -> bool:
    """
    Check if a media reference exists on CDN (read-only, no side effects).

    Uses file/exists endpoint to verify without creating refs.
    Returns True if exists, False if not found.
    On network errors, raises requests.RequestException so caller can decide.
    """
    import requests
    from django.conf import settings

    cdn_url = getattr(settings, "CDN_SERVICE_URL", "http://stapel-cdn:8000")
    api_key = getattr(settings, "SERVICE_API_KEY", "")
    headers = {"X-API-KEY": api_key} if api_key else {}

    if "/" not in ref_str:
        return False

    parts = ref_str.split("/")
    file_hash = parts[-1]

    response = requests.get(
        f"{cdn_url}/cdn/api/file/exists/",
        params={"file_hash": file_hash},
        headers=headers,
        timeout=5,
    )
    if response.status_code == 200:
        data = response.json()
        return data.get("exists", False)
    logger.warning(
        "CDN exists check failed: status=%s ref=%s", response.status_code, ref_str
    )
    raise requests.RequestException(
        f"CDN returned status {response.status_code} for {ref_str}"
    )
