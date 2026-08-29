"""The site registry — one build, N hosts.

A deployment that serves two brands from one image needs exactly one answer to
"which site is this request for?", and it needs it in more places than a view:
``ALLOWED_HOSTS`` and ``CSRF_TRUSTED_ORIGINS`` derive from it, the WebSocket
origin allowlist derives from it, ``return_to``/``redirect_after`` is validated
against it, and the storefront asks for it over HTTP before its first render.
When each of those re-derives the list from its own environment variable they
drift, and the drift is silent until the day one host cannot log in.

So the registry is **deployment configuration read once** (``STAPEL_SITES``, or
a JSON file named by ``STAPEL_SITES_FILE``, or inline ``STAPEL_SITES_JSON``) —
not a table in one service's database, because every service in the fleet needs
it and only one of them would own that table, and because nginx/certbot read
the same file before Django exists.

This module imports **no Django**: it is the pure parse-and-match half, so a
deploy script, a settings module (which runs before ``django.setup()``) and a
view can all use it. The Django half lives in
:mod:`stapel_core.django.sites`.

Shape::

    {"sites": [
      {"host": "example.com", "aliases": ["www.example.com"], "primary": true,
       "locale": "ru",
       "brand": {"key": "acme", "name": "Acme", "title": "Acme — classifieds",
                 "logo": "/brand/acme/logo.svg", "theme": "acme",
                 "legal": {"company": "…", "support_email": "hello@example.com",
                           "privacy_url": "/privacy", "terms_url": "/terms"}},
       "seo": {"index": true}}
    ]}

Every rule the shape carries is enforced at load time and raises
:class:`SitesConfigError`, because the alternative to a loud parse failure is a
deployment that answers requests for a host it does not believe in.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Optional
from urllib.parse import urlsplit

__all__ = [
    "Brand",
    "Site",
    "SiteRegistry",
    "SitesConfigError",
    "load_sites",
    "registry_from_settings",
    "reset_sites_cache",
    "sites_data_from_env",
]

#: ``brand.key`` and ``brand.theme`` become a CSS selector
#: (``:root[data-brand="nord"]``), a directory name and a token scope, so the
#: vocabulary is deliberately narrow — anything else is a quoting bug waiting
#: for a stylesheet.
_KEY_RE = re.compile(r"^[a-z0-9-]+$")

#: A hostname, lowercased. No scheme, no port, no path — those are the three
#: ways an operator writes an origin where a host was asked for, and each one
#: produces a registry entry that can never match a ``Host:`` header.
_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
)

#: ``netloc`` of an admissible origin: host plus an optional numeric port and
#: nothing else. Rejects userinfo (``https://example.com@attacker.test/``), a backslash
#: (which browsers read as a separator and :func:`urlsplit` does not) and every
#: other way of writing a netloc that parses differently in two places.
_NETLOC_RE = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?$")


class SitesConfigError(ValueError):
    """``STAPEL_SITES`` does not describe a registry this process can serve.

    Carries a machine-readable :attr:`code` alongside the human message so the
    system checks can tell the primary rule (``stapel_core.sites.E002``) from
    every other malformed-registry finding (``E001``) without matching on
    prose.
    """

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Brand:
    """The visible identity of a site: what the storefront paints.

    ``legal`` is free-form (``company``, ``support_email``, ``privacy_url``,
    ``terms_url`` in practice) — the registry validates the ``*_url`` entries
    and carries the rest verbatim, so a brand can grow a line without a core
    release.
    """

    key: str
    name: str
    title: str = ""
    logo: str = ""
    theme: str = ""
    legal: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "title": self.title,
            "logo": self.logo,
            "theme": self.theme,
            "legal": dict(self.legal),
        }


@dataclass(frozen=True)
class Site:
    """One host this deployment serves, with the brand it wears there."""

    host: str
    aliases: tuple[str, ...] = ()
    primary: bool = False
    locale: str = ""
    brand: Optional[Brand] = None
    seo: Mapping = field(default_factory=dict)

    @property
    def hosts(self) -> tuple[str, ...]:
        """``host`` and every alias — the names a browser may send."""
        return (self.host, *self.aliases)

    @property
    def origins(self) -> tuple[str, ...]:
        """``https://<name>`` for every name this site answers to."""
        return tuple(f"https://{name}" for name in self.hosts)


class SiteRegistry:
    """The loaded registry: host → :class:`Site`, and the derivations from it.

    Empty is a first-class state, not a degenerate one: a single-host
    deployment declares nothing, ``for_host`` answers ``None``, the settings
    derivation leaves ``STAPEL_HOST`` alone and the bootstrap endpoint reports
    ``matched: false`` with a null brand. Nothing in the fleet has to grow a
    registry to keep working.
    """

    def __init__(self, sites: Sequence[Site] = ()):
        self._sites: tuple[Site, ...] = tuple(sites)
        index: dict[str, Site] = {}
        for site in self._sites:
            for name in site.hosts:
                index[name] = site
        self._index = index

    def __bool__(self) -> bool:
        return bool(self._sites)

    def __len__(self) -> int:
        return len(self._sites)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SiteRegistry {[s.host for s in self._sites]!r}>"

    @property
    def sites(self) -> tuple[Site, ...]:
        return self._sites

    def hosts(self) -> tuple[str, ...]:
        """Every host and alias in the registry, in declaration order."""
        return tuple(self._index)

    def for_host(self, host) -> Optional[Site]:
        """The site serving *host*, matching a host or an alias.

        The port is stripped and the name lowercased before matching: a
        ``Host:`` header carries whatever the browser was pointed at
        (``EXAMPLE.com:8443``), and a registry that only matches the canonical
        spelling is a registry that stops working behind a non-default port.
        """
        return self._index.get(normalize_host(host))

    def primary(self) -> Optional[Site]:
        """The site flagged ``primary`` — or the only site, when there is one.

        The primary is what an unmatched host falls back to and what code with
        no request in hand (a Celery task minting an email link) must use.
        """
        for site in self._sites:
            if site.primary:
                return site
        return self._sites[0] if len(self._sites) == 1 else None

    def origins(self) -> tuple[str, ...]:
        """``https://<name>`` for every host and alias.

        The one list that feeds ``CSRF_TRUSTED_ORIGINS``, the WebSocket origin
        allowlist and the ``return_to`` allowlist — see the module docstring on
        why they must not each build their own.
        """
        return tuple(f"https://{name}" for name in self._index)

    def is_site_origin(self, url) -> bool:
        """Is *url* served by this deployment? The open-redirect gate.

        Parsed, never prefix-matched: ``https://example.com.attacker.test/`` starts with
        a registered host and is a different site, ``http://example.com/`` is the
        right host over the wrong scheme, and ``https://attacker.test/?x=example.com``
        merely mentions one. All three answer ``False``; only an exact
        host-or-alias match over ``https`` on the default port answers ``True``.
        An empty registry answers ``False`` for everything — there is nothing to
        be an origin *of*.
        """
        try:
            parts = urlsplit(str(url).strip())
        except (ValueError, TypeError):
            return False
        if parts.scheme.lower() != "https":
            return False
        netloc = (parts.netloc or "").lower()
        if not _NETLOC_RE.match(netloc):
            return False
        try:
            port = parts.port
        except ValueError:
            return False
        if port not in (None, 443):
            return False
        hostname = (parts.hostname or "").lower()
        return bool(hostname) and hostname in self._index


def normalize_host(host) -> str:
    """``'WWW.Example.COM:8443'`` -> ``'www.example.com'``. Never raises."""
    value = str(host or "").strip().lower()
    if value.startswith("["):  # IPv6 literal — the brackets carry the colons
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    if ":" in value:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def registrable_domain(host) -> str:
    """The last two labels of *host* — ``www.example.com`` -> ``example.com``.

    An approximation of the public-suffix answer, deliberately: the only
    question core asks it is "could one ``Domain=`` cookie cover both of these
    hosts", and for that question two labels is the conservative reading. It is
    never used to *grant* anything.
    """
    labels = normalize_host(host).split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else ".".join(labels)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _require_mapping(value, what: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise SitesConfigError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value


def _validate_url(value, what: str) -> str:
    """A relative ``/path`` or an ``https://`` URL — nothing else.

    A brand asset or legal link is rendered into a page every visitor sees and
    into emails; ``http://`` there is a mixed-content block or a downgrade, and
    ``javascript:``/``data:`` is an injection with a config file for a vector.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        raise SitesConfigError(
            f"{what} is protocol-relative ({text!r}); write a relative path "
            "starting with '/' or a full https:// URL",
            code="url",
        )
    if text.startswith("/"):
        return text
    if text.lower().startswith("https://"):
        return text
    raise SitesConfigError(
        f"{what} must be a relative path ('/privacy') or an https:// URL, got {text!r}",
        code="url",
    )


def _load_brand(raw, host: str) -> Brand:
    data = _require_mapping(raw, f"site {host!r}: 'brand'")
    key = str(data.get("key") or "").strip()
    if not key:
        raise SitesConfigError(f"site {host!r}: brand.key is required", code="brand")
    if not _KEY_RE.match(key):
        raise SitesConfigError(
            f"site {host!r}: brand.key {key!r} must match [a-z0-9-]+ — it becomes "
            "a CSS selector, a directory name and a token scope",
            code="brand",
        )
    theme = str(data.get("theme") or key).strip()
    if not _KEY_RE.match(theme):
        raise SitesConfigError(
            f"site {host!r}: brand.theme {theme!r} must match [a-z0-9-]+ — it is "
            'written into :root[data-brand="…"]',
            code="brand",
        )
    name = str(data.get("name") or key).strip()
    legal_raw = data.get("legal") or {}
    legal = _require_mapping(legal_raw, f"site {host!r}: brand.legal")
    legal_clean: dict[str, str] = {}
    for legal_key, legal_value in legal.items():
        text = "" if legal_value is None else str(legal_value)
        if str(legal_key).endswith("_url"):
            text = _validate_url(text, f"site {host!r}: brand.legal.{legal_key}")
        legal_clean[str(legal_key)] = text
    return Brand(
        key=key,
        name=name,
        title=str(data.get("title") or name),
        logo=_validate_url(data.get("logo"), f"site {host!r}: brand.logo"),
        theme=theme,
        legal=MappingProxyType(legal_clean),
    )


def _declared_host(value, what: str) -> str:
    """A *declared* host: lowercased, never port-stripped.

    ``for_host`` strips the port off an incoming ``Host:`` header, because a
    browser sends one. A registry ENTRY carrying a port (or a scheme, or a
    path) is a different thing — an operator who wrote an origin where a host
    was asked for — and silently trimming it to something that happens to
    parse is how ``https://example.com`` becomes a site named ``https``.
    """
    text = str(value or "").strip().lower().rstrip(".")
    if not text:
        raise SitesConfigError(f"{what} must be a non-empty hostname", code="host")
    if not _HOST_RE.match(text):
        raise SitesConfigError(
            f"{what} {str(value)!r} is not a bare hostname — no scheme, port or "
            "path (write 'example.com', not 'https://example.com/')",
            code="host",
        )
    return text


def _load_site(raw, seen: dict) -> Site:
    data = _require_mapping(raw, "each entry of 'sites'")
    host = _declared_host(data.get("host"), "site 'host'")

    aliases: list[str] = []
    raw_aliases = data.get("aliases") or ()
    if isinstance(raw_aliases, (str, bytes)) or not isinstance(raw_aliases, Sequence):
        raise SitesConfigError(f"site {host!r}: 'aliases' must be a list", code="host")
    for alias in raw_aliases:
        aliases.append(_declared_host(alias, f"site {host!r}: alias"))

    for name in (host, *aliases):
        if name in seen:
            raise SitesConfigError(
                f"host {name!r} is claimed twice (sites {seen[name]!r} and {host!r}); "
                "one name can only resolve to one site",
                code="duplicate",
            )
        seen[name] = host

    brand = _load_brand(data["brand"], host) if data.get("brand") else None
    seo = _require_mapping(data.get("seo") or {}, f"site {host!r}: 'seo'")

    return Site(
        host=host,
        aliases=tuple(aliases),
        primary=bool(data.get("primary", False)),
        locale=str(data.get("locale") or ""),
        brand=brand,
        seo=MappingProxyType(dict(seo)),
    )


def load_sites(data) -> SiteRegistry:
    """Parse and validate the registry. See the module docstring for the shape.

    Accepts ``{"sites": [...]}`` (the file format) or the bare list, and treats
    ``None``/``{}``/``[]`` as "no registry declared". Every violation raises
    :class:`SitesConfigError` — nothing is dropped silently, because a site
    that quietly vanished from the registry is a host that quietly stopped
    being allowed.
    """
    if data is None:
        return SiteRegistry()
    if isinstance(data, Mapping):
        if data and "sites" not in data:
            raise SitesConfigError(
                "the sites registry is an object with a 'sites' list: "
                '{"sites": [{"host": "…"}]}'
            )
        entries = data.get("sites") or []
    elif isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        entries = list(data)
    else:
        raise SitesConfigError(
            f"the sites registry must be an object or a list, got {type(data).__name__}"
        )

    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise SitesConfigError("'sites' must be a list of site objects")

    seen: dict[str, str] = {}
    sites = [_load_site(entry, seen) for entry in entries]

    if len(sites) > 1:
        primaries = [s.host for s in sites if s.primary]
        if not primaries:
            raise SitesConfigError(
                f"{len(sites)} sites are registered and none is marked "
                '"primary": true — the primary is what an unmatched host falls '
                "back to and what code with no request (an email link) uses",
                code="primary",
            )
        if len(primaries) > 1:
            raise SitesConfigError(
                f"{len(primaries)} sites are marked \"primary\": true "
                f"({', '.join(primaries)}) — exactly one site is the fallback",
                code="primary",
            )
    return SiteRegistry(sites)


def sites_data_from_env(environ: Optional[Mapping[str, str]] = None):
    """The registry data an environment declares, or ``{}``.

    ``STAPEL_SITES_FILE`` (a path — how the fleet ships it, since nginx and
    certbot read the same file) wins over ``STAPEL_SITES_JSON`` (inline, for a
    one-liner deployment or a test).
    """
    env = os.environ if environ is None else environ
    path = (env.get("STAPEL_SITES_FILE") or "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except OSError as exc:
            raise SitesConfigError(
                f"STAPEL_SITES_FILE={path!r} cannot be read: {exc}", code="file"
            ) from exc
        except json.JSONDecodeError as exc:
            raise SitesConfigError(
                f"STAPEL_SITES_FILE={path!r} is not valid JSON: {exc}", code="file"
            ) from exc
    inline = (env.get("STAPEL_SITES_JSON") or "").strip()
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as exc:
            raise SitesConfigError(
                f"STAPEL_SITES_JSON is not valid JSON: {exc}", code="file"
            ) from exc
    return {}


_CACHE: Optional[SiteRegistry] = None


def reset_sites_cache() -> None:
    """Drop the per-process registry cache (tests, and ``setting_changed``)."""
    global _CACHE
    _CACHE = None


def registry_from_settings() -> SiteRegistry:
    """The process's registry, parsed once and cached.

    Resolution order: the Django setting ``STAPEL_SITES`` (already parsed —
    ``stapel_core.django.settings`` fills it from the environment at import
    time, and a project may write it by hand), then ``STAPEL_SITES_FILE``, then
    ``STAPEL_SITES_JSON``, then empty.

    Django is imported lazily and its absence is not an error: a deploy script
    calling this before ``django.setup()`` gets the environment answer.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    data = None
    try:
        from django.conf import settings as django_settings

        if django_settings.configured:
            data = getattr(django_settings, "STAPEL_SITES", None)
    except Exception:  # Django absent or misconfigured — env is still readable
        data = None
    if not data:
        data = sites_data_from_env()
    _CACHE = load_sites(data)
    return _CACHE
