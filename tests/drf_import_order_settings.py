"""A settings module shaped like a real project's: DRF imported above the
``REST_FRAMEWORK`` dict.

Run by ``tests/test_drf_rebind.py`` in a subprocess — the trap can only be
sprung by a genuine ``django.setup()`` on a settings module, because what
makes it happen is Django reading a half-built ``Settings`` object while the
module is still executing.

Named settings are chosen so that every one of them differs from DRF's
default and from anything this repo configures elsewhere.
"""
from rest_framework.metadata import SimpleMetadata


class CustomMetadata(SimpleMetadata):
    """A metadata class no default could be mistaken for."""


# The trap. A project writes `import stapel_core.django` (or a star-import of
# stapel_core.django.settings) here, above its own REST_FRAMEWORK.
import stapel_core.django  # noqa: E402,F401

SECRET_KEY = "drf-import-order-settings-not-for-production"
DEBUG = False
ALLOWED_HOSTS = ["*"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "stapel_core.django",
]
MIDDLEWARE = []
ROOT_URLCONF = ""
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_METADATA_CLASS": "drf_import_order_settings.CustomMetadata",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.NamespaceVersioning",
    "PAGE_SIZE": 33,
    "SEARCH_PARAM": "q",
}
