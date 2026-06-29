"""
GDPR primitives shared across all Stapel packages.

Microservices bus protocol
--------------------------
Each service that holds user data must run a GDPR consumer:

    class Command(GDPRServiceConsumerCommand):
        gdpr_service_name = 'auth'      # matches GDPR_COLLECTING_SERVICES entry

        def get_gdpr_provider(self):
            from stapel_auth.gdpr import AuthGDPRProvider
            return AuthGDPRProvider()

The GDPR service declares which services it expects data from:

    # settings.py
    GDPR_COLLECTING_SERVICES = ['auth', 'cdn', 'profiles']

A CI linter (scripts/check_gdpr_services.py) verifies that every entry in
GDPR_COLLECTING_SERVICES has a consumer, and no consumer is unlisted.
"""

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bus event type constants
# ---------------------------------------------------------------------------

GDPR_EXPORT_REQUESTED = 'gdpr.export.requested'
GDPR_EXPORT_COMPLETED = 'gdpr.export.completed'
GDPR_DELETE_REQUESTED = 'gdpr.delete.requested'
GDPR_DELETE_COMPLETED = 'gdpr.delete.completed'


# ---------------------------------------------------------------------------
# GDPRProvider — in-process interface (monolith / same-container mode)
# ---------------------------------------------------------------------------

class GDPRProvider(ABC):
    """Base class for per-app GDPR data handlers."""

    #: Unique section name used as directory name in export archive.
    section: str

    @abstractmethod
    def export(self, user_id: int) -> dict:
        """Return all exportable data for this user as a JSON-serialisable dict."""

    def export_to_staging(self, user_id: int, staging_dir: Path) -> list[Path]:
        """Write exported data to staging_dir. Override for binary files (photos etc.)."""
        data = self.export(user_id)
        out = staging_dir / f'{self.section}.json'
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return [out]

    @abstractmethod
    def delete(self, user_id: int) -> None:
        """Hard-delete all PII for this user (called after grace period)."""

    @abstractmethod
    def anonymize(self, user_id: int) -> None:
        """Anonymize content that must be retained (reviews, chat messages, etc.)."""


# ---------------------------------------------------------------------------
# GDPRRegistry — optional in-process registry (monolith mode)
# ---------------------------------------------------------------------------

class GDPRRegistry:
    def __init__(self):
        self._providers: list[GDPRProvider] = []

    def register(self, provider: GDPRProvider) -> None:
        if not hasattr(provider, 'section') or not provider.section:
            raise ValueError(f'{provider.__class__.__name__} must define a non-empty `section`')
        existing = {p.section for p in self._providers}
        if provider.section in existing:
            raise ValueError(f"GDPRProvider with section '{provider.section}' is already registered")
        self._providers.append(provider)
        logger.debug('GDPR provider registered: %s (section=%s)', provider.__class__.__name__, provider.section)

    @property
    def providers(self) -> list[GDPRProvider]:
        return list(self._providers)

    @property
    def sections(self) -> list[str]:
        return [p.section for p in self._providers]


gdpr_registry = GDPRRegistry()


# ---------------------------------------------------------------------------
# GDPRServiceConsumerCommand — base management command for microservices mode
# ---------------------------------------------------------------------------

class GDPRServiceConsumerCommand:
    """
    Base Django management command for services that handle GDPR bus events.

    Subclass in each service that holds user data:

        class Command(GDPRServiceConsumerCommand):
            gdpr_service_name = 'auth'

            def get_gdpr_provider(self):
                from stapel_auth.gdpr import AuthGDPRProvider
                return AuthGDPRProvider()

    The ``gdpr_service_name`` must match an entry in ``GDPR_COLLECTING_SERVICES``
    on the GDPR service. The CI linter enforces this.
    """

    #: Must match the entry in GDPR_COLLECTING_SERVICES. Used as event `service` field.
    gdpr_service_name: str

    @property
    def topics(self) -> list[str]:
        return [GDPR_EXPORT_REQUESTED, GDPR_DELETE_REQUESTED]

    @property
    def consumer_group(self) -> str:
        return f'gdpr-{self.gdpr_service_name}'

    def get_gdpr_provider(self) -> GDPRProvider:
        raise NotImplementedError

    def handle_gdpr_event(self, event) -> None:
        if event.event_type == GDPR_EXPORT_REQUESTED:
            self._handle_export(event)
        elif event.event_type == GDPR_DELETE_REQUESTED:
            self._handle_delete(event)

    def _handle_export(self, event) -> None:
        user_id = event.payload['user_id']
        correlation_id = event.payload['correlation_id']

        provider = self.get_gdpr_provider()
        bucket_path = self._upload_export(user_id, correlation_id, provider)
        self._publish(GDPR_EXPORT_COMPLETED, {
            'correlation_id': correlation_id,
            'user_id': user_id,
            'bucket_path': bucket_path,
        }, key=str(correlation_id))
        logger.info('GDPR export completed [service=%s user=%s correlation=%s]',
                    self.gdpr_service_name, user_id, correlation_id)

    def _handle_delete(self, event) -> None:
        user_id = event.payload['user_id']
        correlation_id = event.payload['correlation_id']

        provider = self.get_gdpr_provider()
        try:
            provider.anonymize(user_id)
            provider.delete(user_id)
        except Exception as e:
            logger.error('GDPR delete failed [service=%s user=%s]: %s',
                         self.gdpr_service_name, user_id, e)
            raise

        self._publish(GDPR_DELETE_COMPLETED, {
            'correlation_id': correlation_id,
            'user_id': user_id,
        }, key=str(correlation_id))
        logger.info('GDPR delete completed [service=%s user=%s correlation=%s]',
                    self.gdpr_service_name, user_id, correlation_id)

    def _upload_export(self, user_id: int, correlation_id: str, provider: GDPRProvider) -> str:
        """Export data and upload to object storage. Returns the storage path."""
        import json
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        data = provider.export(user_id)
        path = f'gdpr/{correlation_id}/{self.gdpr_service_name}/export.json'
        content = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        if default_storage.exists(path):
            default_storage.delete(path)
        default_storage.save(path, ContentFile(content))
        return path

    def _publish(self, event_type: str, payload: dict, key: str | None = None) -> None:
        from stapel_core.bus.event import Event
        from stapel_core.bus.router import get_bus
        get_bus().publish(event_type, Event(
            event_type=event_type,
            service=self.gdpr_service_name,
            payload=payload,
            key=key,
        ))
