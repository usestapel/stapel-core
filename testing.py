"""
Test configuration helpers for standalone stapel-* packages.

Usage in conftest.py:
    from stapel_core.testing import configure_django
    configure_django(
        installed_apps=[
            'stapel_auth',
            'stapel_auth.migrations',
        ],
    )
"""
import django
from django.conf import settings


BASE_INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
]

BASE_MIDDLEWARE = [
    'django.middleware.common.CommonMiddleware',
]

BASE_REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'stapel_core.django.authentication.JWTCookieAuthentication',
    ],
    # Empty by default — avoid IsAuthenticated/IsServiceRequest blocking tests with 403
    'DEFAULT_PERMISSION_CLASSES': [],
    'EXCEPTION_HANDLER': 'stapel_core.django.errors.iron_exception_handler',
}


def configure_django(
    *,
    installed_apps: list[str],
    extra_settings: dict | None = None,
    middleware: list[str] | None = None,
    rest_framework: dict | None = None,
) -> None:
    """Configure Django for in-process package tests with SQLite.

    Call once from conftest.py before any imports that trigger Django setup.
    Safe to call multiple times — subsequent calls are no-ops if already configured.
    """
    if settings.configured:
        if not django.conf._wrapped:  # type: ignore[attr-defined]
            django.setup()
        return

    all_apps = BASE_INSTALLED_APPS + installed_apps

    settings.configure(
        SECRET_KEY='test-secret-key-not-for-production',
        DATABASES={
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': ':memory:',
            }
        },
        INSTALLED_APPS=all_apps,
        MIDDLEWARE=middleware if middleware is not None else BASE_MIDDLEWARE,
        ROOT_URLCONF='',
        ALLOWED_HOSTS=['*'],
        USE_TZ=True,
        REST_FRAMEWORK=rest_framework if rest_framework is not None else BASE_REST_FRAMEWORK,
        STAPEL_AUTH={
            'JWT_SECRET': 'test-jwt-secret',
            'JWT_ALGORITHM': 'HS256',
            'ACCESS_TOKEN_LIFETIME_SECONDS': 900,
            'REFRESH_TOKEN_LIFETIME_SECONDS': 604800,
        },
        **(extra_settings or {}),
    )
    django.setup()
