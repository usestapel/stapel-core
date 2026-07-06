"""stapel_core.i18n — domain-agnostic shipping of localized content.

The generalization of the flow-i18n contour (``flows/i18n.py``) to arbitrary
content *domains* (i18n-shipping.md). One mechanism ships en + ru (and any
on-demand language) with framework libraries, lets a host add languages or
override texts **without a fork**, and gates the result:

* **catalogs** — per-app ``translations/<domain>.<lang>.json`` (flat
  ``{key: text}``), discovered over INSTALLED_APPS, merged later-wins
  (:func:`load_app_catalogs`);
* **provenance** — a ``.state.json`` sidecar records per key whether a value
  was seeded from a curated corpus, machine-translated (unreviewed) or human
  (:class:`StateSidecar`);
* **write-time generation** — :func:`translate_catalog` (the
  ``translate_catalogs`` command) fills a locale from a seed → the translator
  seam, content-hash cached and byte-stable;
* **the gate** — :func:`check_translation_catalogs` (the
  ``check_translation_catalogs`` command) fails the build on missing / stale /
  params-mismatched entries and counts unreviewed ones.

``STAPEL_I18N`` (:mod:`stapel_core.i18n.conf`) carries the project languages
(``LOCALES``, the single knob ``DOC_LANGUAGES`` delegates to), extra catalog
dirs, and the machine-translation seam. Domains register their canonical
source-text resolver in :mod:`stapel_core.i18n.domains`.
"""
from .catalogs import (
    CATALOG_DIRNAME,
    ORIGIN_HUMAN,
    ORIGIN_LLM,
    STATE_FILENAME,
    CommDocTranslator,
    DocTranslationCache,
    StateSidecar,
    catalog_filename,
    catalog_relpath,
    content_hash,
    dump_catalog,
    is_reviewed,
    load_app_catalogs,
    load_catalog_file,
)
from .check import CatalogIssue, check_translation_catalogs, summarize
from .conf import i18n_settings, project_languages
from .domains import DOMAIN_SOURCES, params_of, source_texts
from .translate import TranslateResult, translate_catalog

__all__ = [
    "CATALOG_DIRNAME",
    "STATE_FILENAME",
    "ORIGIN_HUMAN",
    "ORIGIN_LLM",
    "CatalogIssue",
    "CommDocTranslator",
    "DOMAIN_SOURCES",
    "DocTranslationCache",
    "StateSidecar",
    "TranslateResult",
    "catalog_filename",
    "catalog_relpath",
    "check_translation_catalogs",
    "content_hash",
    "dump_catalog",
    "i18n_settings",
    "is_reviewed",
    "load_app_catalogs",
    "load_catalog_file",
    "params_of",
    "project_languages",
    "source_texts",
    "summarize",
    "translate_catalog",
]
