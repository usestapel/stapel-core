"""0.96.0's seams come up together in one process, in either INSTALLED_APPS order.

The access observer (JWT), the fact-schema ownership (comm) and the sitemap
aggregator each arrived on its own branch with its own tests. This boots a
minimal project twice — core's apps in one order, then reversed — runs
``manage.py check`` and exercises all three in the same process.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

STAPEL_APPS = [
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
    "stapel_core.django.outbox",
    "stapel_core.django.taskstore",
]

SETTINGS = """
from stapel_core.django.settings import *  # noqa: F401,F403

DATABASES = {{"default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}}}
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
] + {apps!r}
AUTH_USER_MODEL = "users.User"
ROOT_URLCONF = "projurls"
CACHES = {{"default": {{"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}}}
STAPEL_BUS_BACKEND = "stapel_core.bus.backends.memory.MemoryBus"
STAPEL_COMM = {{"OUTBOX_ENABLED": False, "ACTION_TRANSPORT": "inprocess"}}
"""

URLS = """
from stapel_core.django.sitemap.urls import get_sitemap_urls

urlpatterns = list(get_sitemap_urls())
"""

SCRIPT = textwrap.dedent("""
    import json
    import django
    django.setup()
    from django.core.management import call_command
    from django.urls import reverse, resolve

    call_command("migrate", verbosity=0)
    call_command("check", fail_level="ERROR")

    from stapel_core.comm import action_registry, emit
    from stapel_core.comm.exceptions import SchemaValidationError
    from stapel_core.testing import assert_module_registers_only_own_emit_schemas
    from stapel_core.gdpr import register_gdpr_owner

    register_gdpr_owner("demo", ["account"], lambda t, k, w=None: {"rows": 0})
    assert_module_registers_only_own_emit_schemas("stapel_core")
    owners = {f: list(o) for f, o in action_registry.emit_declarations().items()}
    try:
        emit("gdpr.section.erased", {"correlation_id": "c", "stray": 1})
        refused = False
    except SchemaValidationError:
        refused = True

    from stapel_core.django.sitemap import register_sitemap_provider
    register_sitemap_provider("demo.sitemap_entries")
    sitemap_path = resolve("/sitemap.xml").url_name

    from django.contrib.auth import get_user_model
    from stapel_core.django.jwt.provider import jwt_provider, register_access_observer
    from stapel_core.django.jwt.utils import serialize_user_to_jwt_data
    seen = []
    register_access_observer(lambda payload, token: seen.append(payload.get("jti")))
    user = get_user_model().objects.create(username="boot", email="boot@example.com")
    _, refresh = jwt_provider.create_tokens_from_data(serialize_user_to_jwt_data(user))
    access = jwt_provider.refresh_access_token(refresh)

    print(json.dumps({
        "owners": owners,
        "refused": refused,
        "sitemap": sitemap_path,
        "observed": bool(access) and len(seen) == 1,
    }))
""")


def _boot(tmp_path, apps):
    (tmp_path / "projsettings.py").write_text(SETTINGS.format(apps=apps))
    (tmp_path / "projurls.py").write_text(URLS)
    env = dict(os.environ, DJANGO_SETTINGS_MODULE="projsettings", PYTHONPATH=str(tmp_path))
    env["SECRET_KEY"] = "boot-order-test-" + "0123456789" * 5
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True,
        env=env, cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("apps", [STAPEL_APPS, list(reversed(STAPEL_APPS))],
                         ids=["core-first", "core-last"])
def test_observer_ownership_and_sitemap_come_up_together(tmp_path, apps):
    state = _boot(tmp_path, apps)
    for fact in ("gdpr.section.erased", "gdpr.owner.alive", "notification.requested"):
        assert state["owners"][fact] == ["stapel_core"], fact
    assert state["refused"] is True
    assert state["sitemap"]
    assert state["observed"] is True
