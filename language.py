"""Effective-language resolution — one answer, asked from anywhere.

Every service needs the same question answered ("what language is this
for?") and until now each answered it differently: `get_language()` here,
a hardcoded "en" there. The resolution is not one lookup — it is a small
state machine over four independent inputs, and the interesting cases are
the ones a single lookup gets wrong.

The four inputs:

    app_language           the user's explicit choice, or None for "auto"
    use_device_language    whether the device should win over that choice
    accept_language        what the device asked for, this request
    auto_detected_language the last language we saw for this user, persisted

`app_language` and `use_device_language` are deliberately separate: a user
who picked Russian may still want the device to win while travelling, and
collapsing the two removes the ability to express that.

`supported_languages` has two modes, and the difference is real:

    a set  → static UI strings exist only in these languages; anything
             else falls back.
    None   → accept whatever was asked for. This is for LLM translation,
             which handles languages no static catalogue contains.

Ported from the marketplace codebase, where this was in service for years
and then quietly failed to make the trip into the framework — with the
result that stapel_notifications resolved every anonymous notification to
a hardcoded "en" (found live by meettoday, 2026-07-28: OTP codes arrived
in English regardless of locale).

Two things were fixed in the move rather than carried over:

  * Accept-Language is now parsed with quality weights. The original took
    the first entry and ignored `q=`, so "de;q=0.2,en;q=0.9" resolved to
    German — the header says the opposite.
  * The region subtag is preserved when the supported set distinguishes
    it, so pt-BR and pt-PT stop collapsing into one.
"""
from __future__ import annotations

#: Last-ditch value for the pure resolver, which is deliberately
#: Django-free. Anything running inside a project should use
#: :func:`default_language` instead — see there for why "en" is not a
#: safe thing for a framework to assume.
DEFAULT_LANGUAGE = "en"


def default_language() -> str:
    """The project's own fallback language.

    A framework that hardcodes "en" as the final answer is imposing a
    product assumption: a service built for a Russian-speaking market
    wants `ru` there, and every English string it ever falls back to is a
    defect. So the value comes from the project, in this order::

        STAPEL_LANGUAGE["DEFAULT"]      explicit, wins
        settings.LANGUAGE_CODE          what Django already knows
        "en"                            only when there is no project

    Preferring Django's own setting means a host that configured its
    language gets the right fallback without learning a second knob —
    the same reasoning as taking LANGUAGE_COOKIE_NAME for the cookie.
    """
    try:
        from django.conf import settings
    except ImportError:  # pragma: no cover - Django-free use
        return DEFAULT_LANGUAGE
    try:
        conf = getattr(settings, "STAPEL_LANGUAGE", None) or {}
        explicit = conf.get("DEFAULT")
        if explicit:
            return explicit
        return getattr(settings, "LANGUAGE_CODE", None) or DEFAULT_LANGUAGE
    except Exception:
        # Settings not configured (a management command, a bare import) —
        # a language lookup must never be the thing that breaks that.
        return DEFAULT_LANGUAGE


def parse_accept_language(header: str | None) -> list[str]:
    """Language tags from an Accept-Language header, best first.

    Quality-weighted, because the header is: "de;q=0.2,en;q=0.9" means
    English, and reading only the first entry gets that backwards. Each
    tag is returned as sent (`pt-BR`), lowercased; callers that want the
    base language take the part before the hyphen.
    """
    if not header:
        return []
    entries: list[tuple[float, int, str]] = []
    for position, part in enumerate(header.split(",")):
        piece = part.strip()
        if not piece:
            continue
        tag, _, params = piece.partition(";")
        tag = tag.strip().lower()
        if not tag or tag == "*":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue  # q=0 means "explicitly not this one"
        # position keeps the header's own order stable among equal weights
        entries.append((-quality, position, tag))
    return [tag for _, _, tag in sorted(entries)]


def _match(tag: str, supported: set[str] | None) -> str | None:
    """The best form of ``tag`` the supported set will accept.

    Tries the full tag first so a set that distinguishes pt-BR from pt-PT
    keeps the distinction, then the base language. Returns None when the
    set has neither. With no set, everything matches as-is.
    """
    if supported is None:
        return tag
    if tag in supported:
        return tag
    base = tag.split("-")[0]
    if base in supported:
        return base
    return None


def _from_header(header: str | None, supported: set[str] | None) -> str | None:
    for tag in parse_accept_language(header):
        matched = _match(tag, supported)
        if matched:
            return matched
    return None


def resolve_language(
    *,
    app_language: str | None = None,
    use_device_language: bool = True,
    accept_language_header: str | None = None,
    supported_languages: set[str] | None = None,
    auto_detected_language: str | None = None,
    default: str = DEFAULT_LANGUAGE,
) -> str:
    """The effective language code.

    Deliberately takes plain values, not a request: the caller that needs
    this most — a notification consumer rendering an email in a different
    process, hours later — has no request to read. It has the user's
    stored preferences, and that is exactly this signature.

    Rules, in order:
      1. explicit choice + device must not win  → the choice
      2. explicit choice + device may win       → device, else the choice
      3. auto + a usable device language        → device
      4. auto + nothing usable                  → last seen, else default
    """
    device = _from_header(accept_language_header, supported_languages)

    if app_language:
        if not use_device_language:
            return app_language
        return device or app_language

    if device:
        return device

    if auto_detected_language:
        matched = _match(auto_detected_language, supported_languages)
        if matched:
            return matched
    return default


def resolve_language_from_request(
    request,
    *,
    supported_languages: set[str] | None = None,
    default: str | None = None,
) -> str:
    """``resolve_language`` fed from a Django request.

    Three transports for the same choice, in order of authority:

      1. **stored preferences** — for someone with an account, the profile
         is the source of truth. Attach them as ``request.language_preferences``
         (any object with ``app_language`` / ``use_device_language`` /
         ``auto_detected_language``).
      2. **an explicit header** — ``X-App-Language``. A mobile client, a
         CLI, or a service-to-service hop has no cookie jar; a header is
         also what survives being forwarded between services.
      3. **a cookie** — how a browser carries an anonymous visitor's
         choice across navigations, where a custom header cannot be set.

    Explicit beats ambient, so a header sent for THIS request outranks a
    cookie left over from an earlier one.

    The cookie name defaults to Django's own ``LANGUAGE_COOKIE_NAME``
    (``django_language``) rather than an invented one: the stock
    ``set_language`` view already writes it, so a host that configured
    Django's language cookie gets this for free instead of maintaining a
    second name for the same thing. Override any of it::

        STAPEL_LANGUAGE = {
            "APP_LANGUAGE_COOKIE": "myapp_lang",
            "USE_DEVICE_LANGUAGE_COOKIE": "myapp_use_device",
            "APP_LANGUAGE_HEADER": "X-My-Language",
        }
    """
    from django.conf import settings

    conf = getattr(settings, "STAPEL_LANGUAGE", None) or {}
    app_cookie = conf.get(
        "APP_LANGUAGE_COOKIE",
        getattr(settings, "LANGUAGE_COOKIE_NAME", "django_language"),
    )
    device_cookie = conf.get("USE_DEVICE_LANGUAGE_COOKIE", "stapel_use_device_language")
    header_name = conf.get("APP_LANGUAGE_HEADER", "X-App-Language")
    meta_key = "HTTP_" + header_name.upper().replace("-", "_")

    meta = getattr(request, "META", {}) or {}
    cookies = getattr(request, "COOKIES", {}) or {}

    app_language = (meta.get(meta_key) or "").strip().lower() or None
    if app_language is None:
        app_language = cookies.get(app_cookie) or None
    use_device_language = cookies.get(device_cookie, "1") != "0"
    auto_detected_language = None

    prefs = getattr(request, "language_preferences", None)
    if prefs is not None:
        app_language = getattr(prefs, "app_language", None) or app_language
        if getattr(prefs, "use_device_language", None) is not None:
            use_device_language = bool(prefs.use_device_language)
        auto_detected_language = getattr(prefs, "auto_detected_language", None)

    meta = getattr(request, "META", {}) or {}
    return resolve_language(
        app_language=app_language,
        use_device_language=use_device_language,
        accept_language_header=meta.get("HTTP_ACCEPT_LANGUAGE"),
        supported_languages=supported_languages,
        auto_detected_language=auto_detected_language,
        default=default or default_language(),
    )


__all__ = [
    "DEFAULT_LANGUAGE",
    "default_language",
    "parse_accept_language",
    "resolve_language",
    "resolve_language_from_request",
]
