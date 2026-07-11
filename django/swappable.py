"""Swappable-class indirection for DAO models and presenters (§55 slice 1).

Config-swap, not config-reshape (``docs/pending/extensibility-presenters.md``
§0/§6): a host replaces a *whole class* — its own model subclass, its own
:class:`~stapel_core.django.api.presenters.Presenter` subclass — through one
dotted-path setting. There is no config-only way to add a field without
writing a class; the ecosystem survey (``tasks/research-django-extensibility.md``)
found no clean precedent for that, so we do not pretend to offer it.

Library code must **never** import a swappable model/presenter directly —
always go through :func:`get_model` / :func:`get_presenter` below. A stray
direct import silently defeats the swap for that call site — this is the
exact bug the research flagged in django-oscar's ``get_class()`` (issue
#3232). The ``SWAP001`` lint (``stapel_tools``, next wave) makes that
discipline machine-checked instead of "remember by hand"; until it ships,
review is the only backstop.

Registry: one settings dict, ``STAPEL_SWAP``, ``{"KEY": "dotted.Path"}``. A
key absent from the dict resolves to the caller's own *default* dotted path
— the common case, so a project that swaps nothing pays zero config cost::

    # host settings.py — replace the core user presenter, keep the model
    STAPEL_SWAP = {
        "USERS_PROFILE_PRESENTER": "myapp.presenters.HostUserPresenter",
    }

Unlike Django's own ``AUTH_USER_MODEL`` / the third-party ``swapper``
package, deciding to swap here carries no migration deadline: Stapel
provisions fresh projects (scaffold-first) and migrates legacy ones from
scratch, so the swap choice is made in the scaffold questionnaire/advisor
*before* the first migration, always (see the governing spec §1 for why that
removes the one real risk the research surfaced for model swapping).

Resolution is cached per key, like :class:`stapel_core.conf.AppSettings`;
:func:`clear_swap_cache` resets it (also wired to Django's
``setting_changed`` test signal, so ``override_settings(STAPEL_SWAP=...)``
in tests just works).
"""
from __future__ import annotations

#: Settings key: ``{"KEY": "dotted.path.To.Class"}`` overrides, by logical key.
SWAP_SETTING = "STAPEL_SWAP"

_cache: dict[str, type] = {}


def _connect_reload() -> None:
    try:
        from django.test.signals import setting_changed

        def _reload(*, setting, **kwargs):
            if setting == SWAP_SETTING:
                clear_swap_cache()

        setting_changed.connect(_reload, weak=False)
    except Exception:  # pragma: no cover - Django not ready yet
        pass


_connect_reload()


def _resolve(key: str, default: str | type) -> type:
    if key in _cache:
        return _cache[key]

    from django.conf import settings
    from django.utils.module_loading import import_string

    overrides = getattr(settings, SWAP_SETTING, None) or {}
    dotted = overrides.get(key, default)
    cls = import_string(dotted) if isinstance(dotted, str) else dotted
    _cache[key] = cls
    return cls


def get_model(key: str, default: str) -> type:
    """Resolve a swappable DAO model class.

    *key* is the logical name the owning library registers (e.g.
    ``"USERS_USER"``); *default* is that library's own dotted path, used
    when ``STAPEL_SWAP`` has no entry for *key*. Decide swaps before the
    first migration (see the module docstring) — this indirection does not
    itself migrate data if you change your mind mid-project.
    """
    return _resolve(key, default)


def get_presenter(key: str, default: str) -> type:
    """Resolve a swappable :class:`~stapel_core.django.api.presenters.Presenter`
    subclass. Same contract as :func:`get_model`; presenters carry no
    migration-deadline risk (they are stateless read views), so this is the
    one point of the two the host is free to swap at any time.
    """
    return _resolve(key, default)


def clear_swap_cache() -> None:
    """Drop the resolved-class cache (tests, or after editing ``STAPEL_SWAP``)."""
    _cache.clear()


__all__ = ["SWAP_SETTING", "get_model", "get_presenter", "clear_swap_cache"]
