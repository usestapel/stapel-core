"""stapel_core.i18n — domain-agnostic shipping of localized content.

The generalization of the flow-i18n contour (``flows/i18n.py``) to arbitrary
content *domains* (i18n-shipping.md). One mechanism ships en + ru (and any
on-demand language) with framework libraries, lets a host add languages or
override texts **without a fork**, and gates the result:

* **catalogs** — per-app ``translations/<domain>.<lang>.json`` (flat
  ``{key: text}``), discovered over INSTALLED_APPS, merged later-wins
  (:func:`load_app_catalogs`);
* **provenance** — a ``.state.json`` sidecar records per key where a value came
  from: ``llm`` / ``seed:<label>`` / ``imported`` / ``human``
  (:class:`StateSidecar`). Only ``human`` counts as reviewed
  (:func:`is_reviewed`) — a curated corpus is still machine-made;
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
    ORIGIN_IMPORTED,
    ORIGIN_LLM,
    ORIGIN_SEED_PREFIX,
    STATE_FILENAME,
    CatalogDirError,
    CommDocTranslator,
    DocTranslationCache,
    StateSidecar,
    catalog_filename,
    catalog_relpath,
    catalog_search_dirs,
    content_hash,
    dump_catalog,
    is_curated,
    is_reviewed,
    is_seeded,
    load_app_catalogs,
    load_catalog_file,
    module_catalog,
    owner_catalog,
    owner_of_dir,
    resolve_catalog_dir,
    seed_origin,
)
from .check import CatalogIssue, check_translation_catalogs, owned_keys, summarize
from .conf import i18n_settings, project_languages
from .domains import DOMAIN_OWNERS, DOMAIN_SOURCES, params_of, source_owners, source_texts
from .translate import TranslateResult, translate_catalog

__all__ = [
    "CATALOG_DIRNAME",
    "STATE_FILENAME",
    "ORIGIN_HUMAN",
    "ORIGIN_IMPORTED",
    "ORIGIN_LLM",
    "ORIGIN_SEED_PREFIX",
    "CatalogDirError",
    "CatalogIssue",
    "CommDocTranslator",
    "DOMAIN_OWNERS",
    "DOMAIN_SOURCES",
    "DocTranslationCache",
    "StateSidecar",
    "TranslateResult",
    "catalog_filename",
    "catalog_relpath",
    "catalog_search_dirs",
    "check_translation_catalogs",
    "content_hash",
    "dump_catalog",
    "i18n_settings",
    "is_curated",
    "is_reviewed",
    "is_seeded",
    "load_app_catalogs",
    "load_catalog_file",
    "module_catalog",
    "owned_keys",
    "owner_catalog",
    "owner_of_dir",
    "params_of",
    "project_languages",
    "resolve_catalog_dir",
    "seed_origin",
    "source_owners",
    "source_texts",
    "summarize",
    "translate_catalog",
]
