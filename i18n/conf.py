"""Settings namespace of the i18n subsystem (``STAPEL_I18N``).

A thin, cross-domain namespace (i18n-shipping.md §2): the *languages of the
project* and where to look for catalogs, plus the machine-translation seam
reused across content domains (flows, errors, …). Deliberately small — the
per-app ``translations/<domain>.<lang>.json`` file convention carries most of
the weight; settings only *extend* it.
"""
from __future__ import annotations

from stapel_core.conf import AppSettings

_MISSING = object()

i18n_settings = AppSettings(
    "STAPEL_I18N",
    defaults={
        # The languages the project ships / documents. The single knob for
        # "project languages": ``DOC_LANGUAGES`` (STAPEL_FLOWS) delegates to
        # this by default (see :func:`project_languages`). en is the canonical
        # source; the rest resolve through catalogs + the translator seam.
        "LOCALES": ["en", "ru"],
        # Extra catalog roots outside the installed apps (a config repo, a
        # mounted volume). Each is treated like an app package dir — catalogs
        # live at ``<dir>/translations/<domain>.<lang>.json``.
        "EXTRA_CATALOG_DIRS": [],
        # The language of the in-code canonical literals / registry texts
        # passed to the translator as the source language.
        "SOURCE_LANGUAGE": "en",
        # The machine-translation seam (i18n-shipping.md §5), reused by
        # ``translate_catalogs`` for every domain — the same dotted-path seam
        # flow docs use, defaulting to the ``llm.translate`` comm Function by
        # name (core never imports the agent package). Protocol:
        #     translate(entries: dict[key, source_text],
        #               source_language: str, target_language: str)
        #         -> dict[key, translated_text]
        "TRANSLATOR": "stapel_core.i18n.catalogs.CommDocTranslator",
    },
    import_strings=("TRANSLATOR",),
    no_env=("TRANSLATOR",),
)


def _explicit(namespace: str, key: str):
    """The host-set value of *key* (namespace dict or flat setting), or _MISSING.

    Unlike ``AppSettings.__getattr__`` this never falls back to a default — it
    reports *whether the host configured the key at all*, which is what soft
    delegation needs.
    """
    from django.conf import settings

    ns = getattr(settings, namespace, None)
    if isinstance(ns, dict) and key in ns:
        return ns[key]
    return getattr(settings, key, _MISSING)


def project_languages() -> list[str]:
    """The project's languages (i18n-shipping.md §2, open question #6).

    Soft delegation: an explicit ``STAPEL_FLOWS["DOC_LANGUAGES"]`` still wins
    (doc languages may legitimately differ from product languages), but when
    the host leaves it unset the single ``STAPEL_I18N["LOCALES"]`` knob drives
    both — no second list to keep in sync.
    """
    explicit = _explicit("STAPEL_FLOWS", "DOC_LANGUAGES")
    if explicit is not _MISSING and explicit:
        return list(explicit)
    return list(i18n_settings.LOCALES)


__all__ = ["i18n_settings", "project_languages"]
