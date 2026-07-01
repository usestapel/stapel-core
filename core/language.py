"""
Language resolution for Stapel services.

Resolves the effective language from cookies, headers, and optional
supported-language constraints.

Cookie contract (set by profiles service):
    - stapel_app_language:        ISO 639-1 code or absent (auto)
    - stapel_use_device_language: "1" / "0"

Cookie names can be overridden via the ``STAPEL_COOKIE_APP_LANGUAGE`` /
``STAPEL_COOKIE_USE_DEVICE_LANGUAGE`` Django settings — e.g. the Iron product
pins them to ``iron_app_language`` / ``iron_use_device_language`` for
backwards compatibility with existing frontends.
"""
from typing import Optional


def _cookie_setting(name: str, default: str) -> str:
    """Read a cookie name from Django settings, falling back to *default*."""
    try:
        from django.conf import settings  # noqa: PLC0415
        return getattr(settings, name, default)
    except Exception:  # noqa: BLE001 — Django not configured
        return default


DEFAULT_LANGUAGE = 'en'

# Cookie names (overridable via Django settings for backwards compatibility).
COOKIE_APP_LANGUAGE = _cookie_setting('STAPEL_COOKIE_APP_LANGUAGE', 'stapel_app_language')
COOKIE_USE_DEVICE_LANGUAGE = _cookie_setting('STAPEL_COOKIE_USE_DEVICE_LANGUAGE', 'stapel_use_device_language')



def parse_accept_language(header: str) -> Optional[str]:
    """Extract primary language code from Accept-Language header.

    "en-US,en;q=0.9,de;q=0.8" → "en"
    """
    if not header:
        return None
    primary = header.split(',')[0].split(';')[0].strip()
    code = primary.split('-')[0].lower()
    return code if code else None


def resolve_language(
    app_language: Optional[str],
    use_device_language: bool,
    accept_language_header: Optional[str],
    supported_languages: Optional[set[str]] = None,
    auto_detected_language: Optional[str] = None,
) -> str:
    """Resolve effective language.

    Args:
        app_language: User's chosen language from profile cookie (None = auto).
        use_device_language: Whether to prefer device language over app_language.
        accept_language_header: Raw Accept-Language HTTP header value.
        supported_languages: If provided, unsupported languages fall back.
            If None, any language from Accept-Language is accepted as-is
            (useful for LLM translations that can handle any language).
        auto_detected_language: Last detected language from profile
            (used as fallback when no Accept-Language header in auto mode).

    Returns:
        Resolved ISO 639-1 language code.

    Resolution rules:
        1. app_language=None, no Accept-Language → auto_detected_language → "en"
        2. app_language=None, Accept-Language exists → device lang,
           fallback to "en" if unsupported (when supported_languages given)
        3. app_language=set, use_device_language=True → device lang,
           fallback to app_language if unsupported (when supported_languages given)
        4. app_language=set, use_device_language=False → app_language
    """
    device_lang = parse_accept_language(accept_language_header or '')

    # Case 4: explicit app language, don't use device
    if app_language and not use_device_language:
        return app_language

    # Case 3: explicit app language, prefer device
    if app_language and use_device_language:
        if not device_lang:
            return app_language
        if supported_languages is None:
            return device_lang
        return device_lang if device_lang in supported_languages else app_language

    # Cases 1 & 2: auto (no app_language)
    if not device_lang:
        # Fallback to last detected language from profile
        fallback = auto_detected_language or DEFAULT_LANGUAGE
        if supported_languages is None or not auto_detected_language:
            return fallback
        return fallback if fallback in supported_languages else DEFAULT_LANGUAGE
    if supported_languages is None:
        return device_lang
    return device_lang if device_lang in supported_languages else DEFAULT_LANGUAGE


def resolve_language_from_request(request, supported_languages: Optional[set[str]] = None) -> str:
    """Django convenience wrapper — reads cookies and headers from request.

    Args:
        request: Django HttpRequest.
        supported_languages: Optional set of supported language codes.
            When None, no fallback is applied (any language is accepted).

    Returns:
        Resolved ISO 639-1 language code.
    """
    app_language = request.COOKIES.get(COOKIE_APP_LANGUAGE) or None
    use_device_raw = request.COOKIES.get(COOKIE_USE_DEVICE_LANGUAGE, '1')
    use_device_language = use_device_raw != '0'
    accept_language_header = request.META.get('HTTP_ACCEPT_LANGUAGE', '')

    return resolve_language(
        app_language=app_language,
        use_device_language=use_device_language,
        accept_language_header=accept_language_header,
        supported_languages=supported_languages,
    )
