"""Language resolution — the cases a single lookup gets wrong."""
import pytest

from stapel_core.language import (
    parse_accept_language,
    resolve_language,
    resolve_language_from_request,
)


class TestAcceptLanguageParsing:
    def test_quality_weights_beat_header_order(self):
        """The original marketplace parser took the first entry and ignored
        q= — so this header resolved to German, the opposite of what it
        says."""
        assert parse_accept_language("de;q=0.2,en;q=0.9")[0] == "en"

    def test_equal_weights_keep_header_order(self):
        assert parse_accept_language("fr,de") == ["fr", "de"]

    def test_q_zero_means_explicitly_not_this_one(self):
        assert "de" not in parse_accept_language("en,de;q=0")

    def test_region_is_preserved_not_flattened(self):
        assert parse_accept_language("pt-BR,en") == ["pt-br", "en"]

    def test_wildcard_and_empty_are_not_languages(self):
        assert parse_accept_language("*") == []
        assert parse_accept_language("") == []
        assert parse_accept_language(None) == []

    def test_malformed_quality_does_not_raise(self):
        assert parse_accept_language("en;q=abc,de") == ["de"]


class TestTheFourRules:
    def test_explicit_choice_wins_when_device_must_not(self):
        assert resolve_language(
            app_language="ru",
            use_device_language=False,
            accept_language_header="en",
        ) == "ru"

    def test_device_wins_when_allowed(self):
        """Not a redundant flag: someone who picked Russian may still want
        the device to win while travelling."""
        assert resolve_language(
            app_language="ru",
            use_device_language=True,
            accept_language_header="en",
        ) == "en"

    def test_device_falls_back_to_the_choice_when_unsupported(self):
        assert resolve_language(
            app_language="ru",
            use_device_language=True,
            accept_language_header="ja",
            supported_languages={"ru", "en"},
        ) == "ru"

    def test_auto_uses_the_device(self):
        assert resolve_language(accept_language_header="de") == "de"

    def test_auto_with_no_header_uses_the_remembered_language(self):
        """The case with no request at all — a notification consumer
        rendering an email hours later."""
        assert resolve_language(auto_detected_language="ru") == "ru"

    def test_auto_with_nothing_at_all_uses_the_default(self):
        assert resolve_language() == "en"

    def test_remembered_language_is_still_subject_to_support(self):
        assert resolve_language(
            auto_detected_language="ja", supported_languages={"ru", "en"}
        ) == "en"


class TestTheTwoModesOfSupportedLanguages:
    """The distinction the framework did not have: static UI strings exist
    in a fixed set, LLM translation does not."""

    def test_a_set_constrains(self):
        assert resolve_language(
            accept_language_header="ja", supported_languages={"ru", "en"}
        ) == "en"

    def test_none_accepts_anything(self):
        assert resolve_language(
            accept_language_header="ja", supported_languages=None
        ) == "ja"

    def test_region_survives_when_the_set_distinguishes_it(self):
        assert resolve_language(
            accept_language_header="pt-BR",
            supported_languages={"pt-br", "pt-pt", "en"},
        ) == "pt-br"

    def test_region_collapses_when_the_set_does_not(self):
        assert resolve_language(
            accept_language_header="pt-BR", supported_languages={"pt", "en"}
        ) == "pt"


class _Request:
    def __init__(self, cookies=None, header=None, prefs=None, app_header=None):
        self.COOKIES = cookies or {}
        self.META = {}
        if header:
            self.META["HTTP_ACCEPT_LANGUAGE"] = header
        if app_header:
            self.META["HTTP_X_APP_LANGUAGE"] = app_header
        if prefs is not None:
            self.language_preferences = prefs


class _Prefs:
    def __init__(self, app_language=None, use_device_language=None,
                 auto_detected_language=None):
        self.app_language = app_language
        self.use_device_language = use_device_language
        self.auto_detected_language = auto_detected_language


@pytest.mark.django_db
class TestFromRequest:
    def test_cookie_carries_an_anonymous_choice(self):
        req = _Request(cookies={"django_language": "ru",
                                "stapel_use_device_language": "0"},
                       header="en")
        assert resolve_language_from_request(req) == "ru"

    def test_stored_preferences_outrank_the_cookie(self):
        """The cookie is how an anonymous visitor carries a choice, not the
        source of truth for someone who has an account."""
        req = _Request(
            cookies={"django_language": "ru", "stapel_use_device_language": "0"},
            header="en",
            prefs=_Prefs(app_language="de", use_device_language=False),
        )
        assert resolve_language_from_request(req) == "de"

    def test_cookie_names_are_configurable(self, settings):
        """A framework must not stamp one product's brand on every
        deployment's cookies."""
        settings.STAPEL_LANGUAGE = {
            "APP_LANGUAGE_COOKIE": "myapp_lang",
            "USE_DEVICE_LANGUAGE_COOKIE": "myapp_use_device",
        }
        req = _Request(cookies={"myapp_lang": "fr", "myapp_use_device": "0"},
                       header="en")
        assert resolve_language_from_request(req) == "fr"
        # ...and the default name is then NOT consulted
        stale = _Request(cookies={"django_language": "fr"}, header="en")
        assert resolve_language_from_request(stale) == "en"

    def test_a_request_without_anything_falls_back(self):
        assert resolve_language_from_request(_Request()) == "en"


@pytest.mark.django_db
class TestTransportAuthority:
    """Three transports, one choice. Explicit beats ambient."""

    def test_default_cookie_is_djangos_own_name(self):
        """Not an invented parallel name: Django's stock `set_language`
        view already writes LANGUAGE_COOKIE_NAME, so a host that
        configured the language cookie gets this for free."""
        req = _Request(cookies={"django_language": "ru",
                                "stapel_use_device_language": "0"})
        assert resolve_language_from_request(req) == "ru"

    def test_header_works_where_there_is_no_cookie_jar(self):
        """A mobile client or a service-to-service hop has no cookies."""
        req = _Request(app_header="ru", header="en",
                       cookies={"stapel_use_device_language": "0"})
        assert resolve_language_from_request(req) == "ru"

    def test_header_outranks_a_leftover_cookie(self):
        req = _Request(app_header="de",
                       cookies={"django_language": "ru",
                                "stapel_use_device_language": "0"})
        assert resolve_language_from_request(req) == "de"

    def test_stored_preferences_outrank_the_header(self):
        req = _Request(app_header="de",
                       prefs=_Prefs(app_language="ru", use_device_language=False))
        assert resolve_language_from_request(req) == "ru"

    def test_header_name_is_configurable(self, settings):
        settings.STAPEL_LANGUAGE = {"APP_LANGUAGE_HEADER": "X-My-Language"}
        req = _Request(cookies={"stapel_use_device_language": "0"})
        req.META["HTTP_X_MY_LANGUAGE"] = "fr"
        assert resolve_language_from_request(req) == "fr"
