"""
GDPR primitives shared across all Stapel packages.

Each Stapel app implements GDPRProvider and registers it in AppConfig.ready():

    from stapel_core.gdpr import gdpr_registry
    from .gdpr import MyAppGDPRProvider
    gdpr_registry.register(MyAppGDPRProvider())
"""

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


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
