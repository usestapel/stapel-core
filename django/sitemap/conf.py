"""Settings namespace of the sitemap aggregator (``STAPEL_SITEMAP``)."""
from stapel_core.conf import AppSettings

#: Prefix of every environment variable this namespace reads.
ENV_PREFIX = "STAPEL_SITEMAP_"


class SitemapAppSettings(AppSettings):
    """``STAPEL_SITEMAP``, read from the environment only under prefixed names.

    The keys (``ORIGIN``, ``CACHE_TTL``, ``PROVIDERS``, …) are too generic to
    trust as bare environment variables: an unrelated ``ORIGIN`` in a pod
    would silently become every sitemap URL's host. Each key is read as
    ``STAPEL_SITEMAP_<KEY>`` and its bare name is never consulted. Coercion,
    blank-means-unset and the env checks (E002/E004/W002) follow the names
    returned here.
    """

    def env_var_names(self, key: str) -> tuple[str, ...]:
        return (f"{ENV_PREFIX}{key}",)


sitemap_settings = SitemapAppSettings(
    "STAPEL_SITEMAP",
    defaults={
        # {"<name>": "<module>.sitemap_entries"} MERGED over the providers
        # registered in code (register_sitemap_provider); None removes one.
        "PROVIDERS": {},
        # Seconds a built sitemap set is served from the cache.
        "CACHE_TTL": 3600,
        # Seconds a set built while a provider did not answer is kept: short,
        # so a provider that comes back is listed again soon.
        "PARTIAL_CACHE_TTL": 300,
        "CACHE_ALIAS": "default",
        # Per-file limits of the sitemaps protocol (50 000 URLs, 50 MiB
        # uncompressed). A page closes at whichever comes first.
        "PAGE_MAX_URLS": 50000,
        "PAGE_MAX_BYTES": 50 * 1024 * 1024,
        # `limit` sent to a provider per call.
        "BATCH_LIMIT": 1000,
        # Ceiling on URLs per site across all providers; the build stops at
        # it and logs "truncated at N".
        "MAX_URLS": 1000000,
        # Seconds one provider call may take before it counts as unanswered.
        "CALL_TIMEOUT": 5.0,
        # How a provider's site-relative path becomes a URL. Placeholders:
        # {origin} ("https://<site host>"), {path}, {locale}. A frontend that
        # prefixes locales sets "{origin}/{locale}{path}".
        "URL_TEMPLATE": "{origin}{path}",
        # Origin when the site registry is empty ("https://example.com");
        # empty = the request's own scheme and host.
        "ORIGIN": "",
    },
)

__all__ = ["ENV_PREFIX", "SitemapAppSettings", "sitemap_settings"]
