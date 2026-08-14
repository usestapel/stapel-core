"""The shipped settings must not serve credentials over cleartext.

``django/settings.py`` is what a Stapel service gets by writing
``from stapel_core.django.settings import *``. It used to ship
``JWT_COOKIE_SECURE=False``, ``SESSION_COOKIE_SECURE = False  # set True in
prod``, no ``CSRF_COOKIE_SECURE`` at all, and — worst of the four —
``SECURE_PROXY_SSL_HEADER`` set unconditionally, which lets any client that
can reach the process declare its own connection secure.

The module cannot be imported into this process (it is a settings module, and
this process already has one), so each scenario boots a fresh interpreter the
way a real project does: a settings module that star-imports it, then
``django.setup()``.
"""
import os
import subprocess
import sys
import textwrap

PROJECT_SETTINGS = """
from stapel_core.django.settings import *  # noqa: F401,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
]
ROOT_URLCONF = "projurls"
"""

PROJECT_URLS = "urlpatterns = []\n"


def _boot(tmp_path, body, env=None):
    """Run *body* against a project settings module that star-imports ours."""
    (tmp_path / "projsettings.py").write_text(PROJECT_SETTINGS, encoding="utf-8")
    (tmp_path / "projurls.py").write_text(PROJECT_URLS, encoding="utf-8")
    child_env = dict(os.environ)
    child_env["DJANGO_SETTINGS_MODULE"] = "projsettings"
    # tmp_path only: the repo root holds a ``django/`` package directory that
    # would shadow Django itself in the child interpreter. stapel_core is
    # resolved from the installed distribution, as a real project resolves it.
    child_env["PYTHONPATH"] = str(tmp_path)
    # A stray value from the developer's own shell must not decide the test.
    for name in (
        "JWT_COOKIE_SECURE",
        "SESSION_COOKIE_SECURE",
        "CSRF_COOKIE_SECURE",
        "STAPEL_TRUST_PROXY_SSL_HEADER",
    ):
        child_env.pop(name, None)
    child_env.update(env or {})
    script = "import django\ndjango.setup()\n" + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(tmp_path), env=child_env,
    )


def test_cookie_flags_ship_tls_only(tmp_path):
    result = _boot(tmp_path, """
        from django.conf import settings
        assert settings.JWT_COOKIE_SECURE is True, settings.JWT_COOKIE_SECURE
        assert settings.SESSION_COOKIE_SECURE is True, settings.SESSION_COOKIE_SECURE
        assert settings.CSRF_COOKIE_SECURE is True, settings.CSRF_COOKIE_SECURE
        print("OK")
    """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_cookie_flags_can_be_opened_explicitly(tmp_path):
    """Plain-HTTP hosts exist; opening is an explicit act, not the default."""
    result = _boot(tmp_path, """
        from django.conf import settings
        assert settings.JWT_COOKIE_SECURE is False
        assert settings.SESSION_COOKIE_SECURE is False
        assert settings.CSRF_COOKIE_SECURE is False
        print("OK")
    """, env={
        "JWT_COOKIE_SECURE": "False",
        "SESSION_COOKIE_SECURE": "False",
        "CSRF_COOKIE_SECURE": "False",
    })
    assert result.returncode == 0, result.stderr


def test_forwarded_proto_is_not_trusted_by_default(tmp_path):
    """X-Forwarded-Proto is a request header. Trusting it unconditionally lets
    a caller that reaches the process directly claim HTTPS, which turns every
    is_secure() decision into whatever the caller wanted."""
    result = _boot(tmp_path, """
        from django.conf import settings
        assert settings.SECURE_PROXY_SSL_HEADER is None, settings.SECURE_PROXY_SSL_HEADER
        assert settings.STAPEL_TRUST_PROXY_SSL_HEADER is False

        from django.test import RequestFactory
        request = RequestFactory().get("/", HTTP_X_FORWARDED_PROTO="https")
        assert request.is_secure() is False, "client-claimed HTTPS was believed"
        print("OK")
    """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_forwarded_proto_is_trusted_when_the_deployment_says_so(tmp_path):
    """Behind a proxy that overwrites the header, the trust is real — and
    stating it is one environment variable."""
    result = _boot(tmp_path, """
        from django.conf import settings
        assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")

        from django.test import RequestFactory
        request = RequestFactory().get("/", HTTP_X_FORWARDED_PROTO="https")
        assert request.is_secure() is True
        print("OK")
    """, env={"STAPEL_TRUST_PROXY_SSL_HEADER": "True"})
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_jwt_cookie_helper_defaults_to_secure_without_the_setting(tmp_path):
    """A service that configures Django by hand, without our settings module,
    must not get a cleartext cookie from the library's own fallback."""
    result = _boot(tmp_path, """
        from django.conf import settings
        from django.http import HttpResponse
        from stapel_core.django.jwt.utils import set_jwt_cookies

        del settings.JWT_COOKIE_SECURE
        response = HttpResponse()
        set_jwt_cookies(response, "access-token", "refresh-token")
        assert all(c["secure"] for c in response.cookies.values()), response.cookies
        print("OK")
    """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# The OpenAPI document is not anonymous reading
# ---------------------------------------------------------------------------

SCHEMA_PROBE = """
    from django.conf import settings
    from django.test import RequestFactory
    from drf_spectacular.views import SpectacularAPIView

    view = SpectacularAPIView.as_view()
    response = view(RequestFactory().get("/schema/"))
    print("STATUS", response.status_code)
    print("PERMS", settings.SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"])
    print("FLAG", getattr(settings, "STAPEL_PUBLIC_API_SCHEMA", None))
"""


def test_schema_is_not_anonymous_by_default(tmp_path):
    """SERVE_PERMISSIONS was AllowAny, so the OpenAPI document — every route,
    every payload shape, and (via PermissionAwareAutoSchema) each endpoint's
    permission classes — was readable by anyone who could reach the service."""
    result = _boot(tmp_path, SCHEMA_PROBE)
    assert result.returncode == 0, result.stderr
    # 401 (not authenticated) rather than 403 — either way, not the document.
    assert "STATUS 401" in result.stdout, result.stdout
    assert "FLAG False" in result.stdout
    assert "AllowAny" not in result.stdout


def test_schema_can_be_published_explicitly(tmp_path):
    """A genuinely public API is a real thing — it just has to say so."""
    result = _boot(
        tmp_path, SCHEMA_PROBE, env={"STAPEL_PUBLIC_API_SCHEMA": "True"},
    )
    assert result.returncode == 0, result.stderr
    assert "STATUS 200" in result.stdout, result.stdout
    assert "AllowAny" in result.stdout
