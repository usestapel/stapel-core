"""Share-link tokens never reach a log line written by the process itself.

A client fleet masked `/share/<token>` at its edge proxy and still found the
tokens in every service's WSGI access log (request line and referer) and in
Django's `Not Found:` warnings — both are written inside the service, after
the proxy.
"""
import logging

import pytest

from stapel_core.observability import url_secrets
from stapel_core.observability.url_secrets import (
    REDACTED,
    UrlSecretFilter,
    redact_url_secrets,
)

TOKEN = "Zx81_kQ-3vPq9LmN"


class TestRedact:
    @pytest.mark.parametrize(
        "text,expected",
        [
            (f"/share/{TOKEN}", f"/share/{REDACTED}"),
            (f"/recordings/api/public/shares/{TOKEN}/unlock", f"/recordings/api/public/shares/{REDACTED}/unlock"),
            (f"GET /recordings/api/public/shares/{TOKEN}?x=1 HTTP/1.1", f"GET /recordings/api/public/shares/{REDACTED}?x=1 HTTP/1.1"),
            (f"https://app.example/share/{TOKEN}#t=3", f"https://app.example/share/{REDACTED}#t=3"),
            ("/recordings/api/recordings/42/shares", "/recordings/api/recordings/42/shares"),
            ("/api/me/", "/api/me/"),
        ],
    )
    def test_the_segment_after_a_secret_name_is_replaced(self, text, expected):
        assert redact_url_secrets(text, segments=("share", "shares")) == expected

    def test_no_segments_configured_changes_nothing(self):
        assert redact_url_secrets(f"/share/{TOKEN}", segments=()) == f"/share/{TOKEN}"

    def test_the_setting_is_read(self, settings):
        settings.STAPEL_OBSERVABILITY = {"SECRET_PATH_SEGMENTS": ["links"]}
        assert redact_url_secrets(f"/docs/links/{TOKEN}") == f"/docs/links/{REDACTED}"
        assert redact_url_secrets(f"/share/{TOKEN}") == f"/share/{TOKEN}"

    def test_non_strings_pass_through(self):
        assert redact_url_secrets(404) == 404
        assert redact_url_secrets(None) is None


def _record(msg, args):
    return logging.LogRecord("x", logging.WARNING, __file__, 1, msg, args, None)


class TestFilter:
    def test_django_not_found_argument(self):
        record = _record("Not Found: %s", (f"/recordings/api/public/shares/{TOKEN}",))
        assert UrlSecretFilter().filter(record) is True
        assert TOKEN not in record.getMessage()
        assert REDACTED in record.getMessage()

    def test_a_dict_of_atoms_is_redacted_value_by_value(self):
        atoms = {"r": f"GET /share/{TOKEN} HTTP/1.1", "f": f"https://app.example/share/{TOKEN}", "s": "200"}
        record = _record("%(r)s %(s)s %(f)s", atoms)
        UrlSecretFilter().filter(record)
        assert TOKEN not in record.getMessage()
        assert record.args["s"] == "200"

    def test_the_real_gunicorn_access_line(self, caplog):
        """The access logger as gunicorn itself calls it, referer included."""
        glogging = pytest.importorskip("gunicorn.glogging")
        from gunicorn.config import Config

        cfg = Config()
        cfg.set("accesslog", "-")
        log = glogging.Logger(cfg)
        url_secrets.install(["gunicorn.access"])

        class Resp:
            status = "200 OK"
            sent = 10
            headers = ()
            response_length = 10

        class Req:
            headers = [("REFERER", f"https://app.example/share/{TOKEN}")]

        environ = {
            "REQUEST_METHOD": "GET",
            "RAW_URI": f"/recordings/api/public/shares/{TOKEN}",
            "PATH_INFO": f"/recordings/api/public/shares/{TOKEN}",
            "QUERY_STRING": "",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "REMOTE_ADDR": "10.0.0.1",
            "HTTP_REFERER": f"https://app.example/share/{TOKEN}",
            "HTTP_USER_AGENT": "test",
        }
        import datetime

        seen = []

        class Grab(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())

        handler = Grab()
        log.access_log.addHandler(handler)
        try:
            log.access(Resp(), Req(), environ, datetime.timedelta(seconds=0.01))
        finally:
            log.access_log.removeHandler(handler)

        assert seen, "gunicorn wrote no access line"
        assert TOKEN not in seen[0]
        assert f"/recordings/api/public/shares/{REDACTED}" in seen[0]
        assert f"/share/{REDACTED}" in seen[0]

    def test_install_is_idempotent(self):
        name = "stapel.test.url_secrets"
        url_secrets.install([name])
        url_secrets.install([name])
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, UrlSecretFilter)]
        assert len(filters) == 1

