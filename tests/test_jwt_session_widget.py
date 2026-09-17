"""The admin session-timeout widget polls a path that still exists.

``static/admin/js/jwt_session.js`` (loaded on every admin page via
``base_site.html``) used to hardcode the pre-v1 literal
``/auth/api/jwt/status/`` — a path AUTH-02 (2026-08-24, a fleet host) retired
for good, since the paired refresh mount had no tracked-session requirement.
One open admin tab polling it every 30s logged 804 404s in a week (2026-09-18
log audit) with no backoff: a permanent 404 answer polled forever, which is a
defect independent of the stale URL itself.

No JS runtime lives in this suite, so this pins the two facts that would let
the bug recur silently: the constants name the v1-mounted path (not the
retired pre-v1 literal), and a consecutive-404 breaker actually stops the
poll interval rather than only logging.
"""
from pathlib import Path

import stapel_core

_WIDGET_JS = (
    Path(stapel_core.__file__).resolve().parent / "static" / "admin" / "js" / "jwt_session.js"
).read_text()


def test_status_and_refresh_urls_use_the_v1_canon():
    assert "'/auth/api/v1/jwt/status/'" in _WIDGET_JS
    assert "'/auth/api/v1/token/refresh/'" in _WIDGET_JS


def test_the_retired_pre_v1_literal_is_gone():
    assert "/auth/api/jwt/status/" not in _WIDGET_JS
    assert "/auth/api/jwt/refresh/" not in _WIDGET_JS


def test_a_permanent_404_stops_the_poll_instead_of_retrying_forever():
    assert "MAX_CONSECUTIVE_404S" in _WIDGET_JS
    assert "clearInterval(pollTimer)" in _WIDGET_JS
    # The interval handle used by the breaker must be the SAME one the
    # init path assigns — a second, untracked setInterval would make the
    # breaker a no-op.
    assert "pollTimer = setInterval(check, CHECK_INTERVAL)" in _WIDGET_JS
