"""
CDN reference sync utility.

Syncs media references with CDN and doubles as validation —
the response contains `errors` list with hashes that CDN couldn't resolve.
"""

import logging
from dataclasses import dataclass, field
from typing import List

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_cdn_url():
    return getattr(settings, "CDN_SERVICE_URL", "http://iron-cdn:8000")


def _service_headers():
    headers = {}
    api_key = getattr(settings, "SERVICE_API_KEY", "")
    if api_key:
        headers["X-API-KEY"] = api_key
    return headers


@dataclass
class RefSyncResult:
    """Result of a CDN ref sync call."""

    added: int = 0
    removed: int = 0
    errors: List[str] = field(default_factory=list)
    ok: bool = True  # False if CDN was unreachable


def sync_cdn_refs(service, entity_type, entity_id, old_refs, new_refs) -> RefSyncResult:
    """
    POST to /cdn/api/refs/sync/.

    Returns RefSyncResult with `errors` containing hashes that CDN couldn't find.
    If CDN is unreachable, returns ok=False (caller decides whether to block).

    Args:
        service: Service name (e.g. 'profiles')
        entity_type: Entity type (e.g. 'ad', 'ad_draft', 'review', 'profile')
        entity_id: Entity identifier (string or int)
        old_refs: List of previous media references (e.g. ['product/hash1'])
        new_refs: List of current media references (e.g. ['product/hash2'])
    """
    old_set = set(old_refs or [])
    new_set = set(new_refs or [])

    if old_set == new_set:
        return RefSyncResult()

    payload = {
        "service": service,
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "old_hashes": list(old_set),
        "new_hashes": list(new_set),
    }

    try:
        response = requests.post(
            f"{_get_cdn_url()}/cdn/api/refs/sync/",
            json=payload,
            headers=_service_headers(),
            timeout=5,
        )
        if response.status_code == 200:
            data = response.json()
            return RefSyncResult(
                added=data.get("added", 0),
                removed=data.get("removed", 0),
                errors=data.get("errors", []),
                ok=True,
            )
        else:
            logger.warning(
                "CDN ref sync failed: status=%s service=%s entity=%s/%s",
                response.status_code,
                service,
                entity_type,
                entity_id,
            )
            return RefSyncResult(ok=False)
    except requests.RequestException as e:
        logger.warning(
            "CDN ref sync error: %s service=%s entity=%s/%s",
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
    if "/" not in ref_str:
        return False

    parts = ref_str.split("/")
    file_hash = parts[-1]

    response = requests.get(
        f"{_get_cdn_url()}/cdn/api/file/exists/",
        params={"file_hash": file_hash},
        headers=_service_headers(),
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
