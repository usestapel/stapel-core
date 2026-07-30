"""#145: ``STAPEL_VERIFICATION['EXTRA_FACTORS']`` must work by *declaration*.

``load_configured_factors()`` is documented (MODULE.md §Verification) as the
host-project escape hatch for substituting or adding a verification factor.
Until 0.16.1 nothing in the framework called it: a host that followed the
documentation to the letter got a decorative setting and no warning at all
(meettoday #124 had to call the loader from its own app layer, or its
security fix would have been a prop).

The assertions below look at the **live registry after boot**, never at the
declared class — that is the only thing that distinguishes "wired" from
"documented".

Django can be configured once per process and the suite's conftest already
did it, so each scenario boots a fresh interpreter.
"""
import os
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOST_FACTOR_MODULE = '''
from stapel_core.verification import VerificationFactor


class HostEmailFactor(VerificationFactor):
    """Host override of the id stapel-auth also registers."""

    id = "otp_email"
    strength = "weak"
    marker = "host"

    def verify(self, user, challenge, payload):
        return False


class HostExtraFactor(VerificationFactor):
    """A brand-new id no library ships."""

    id = "host_sms"
    strength = "strong"

    def verify(self, user, challenge, payload):
        return False
'''

BOOT = '''
import os, sys
sys.path = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != os.path.abspath({repo!r})]
sys.path.insert(0, {modules_dir!r})
import django
from django.conf import settings
settings.configure(
    SECRET_KEY="x",
    DATABASES={{"default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}}},
    INSTALLED_APPS={apps!r},
    AUTH_USER_MODEL="users.User",
    ROOT_URLCONF="", ALLOWED_HOSTS=["*"], USE_TZ=True,
    STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
    CACHES={{"default": {{"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}}},
    STAPEL_VERIFICATION={extra!r},
)
django.setup()
{body}
'''


def _boot(tmp_path, apps, extra, body, module_source=HOST_FACTOR_MODULE):
    (tmp_path / "hostfactors.py").write_text(module_source, encoding="utf-8")
    script = BOOT.format(
        repo=REPO_ROOT,
        modules_dir=str(tmp_path),
        apps=apps,
        extra=extra,
        body=textwrap.dedent(body),
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


CORE_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
    "stapel_core.django.outbox",
]


def _has_auth() -> bool:
    import importlib.util

    return importlib.util.find_spec("stapel_auth") is not None


def test_declared_extra_factor_is_live_after_boot(tmp_path):
    """Declaring the dotted path is enough — no app-layer call site."""
    result = _boot(
        tmp_path,
        CORE_APPS,
        {"EXTRA_FACTORS": ["hostfactors.HostExtraFactor"]},
        """
        from stapel_core.verification import factor_registry
        live = factor_registry.get("host_sms")
        assert type(live).__name__ == "HostExtraFactor", type(live)
        assert "host_sms" in factor_registry.names(), factor_registry.names()
        assert "host_sms" in factor_registry.strong_names()
        print("EXTRA_FACTOR_LIVE_OK")
        """,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "EXTRA_FACTOR_LIVE_OK" in result.stdout


def test_no_extra_factors_declared_is_a_no_op(tmp_path):
    result = _boot(
        tmp_path,
        CORE_APPS,
        {},
        """
        from stapel_core.verification import factor_registry
        assert factor_registry.names() == [], factor_registry.names()
        assert factor_registry.pinned_names() == []
        print("NO_EXTRA_OK")
        """,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "NO_EXTRA_OK" in result.stdout


def test_unimportable_dotted_path_fails_loudly_at_boot(tmp_path):
    result = _boot(
        tmp_path,
        CORE_APPS,
        {"EXTRA_FACTORS": ["hostfactors.NoSuchFactor"]},
        """
        print("SHOULD_NOT_REACH")
        """,
    )
    assert result.returncode != 0, result.stdout
    assert "ImproperlyConfigured" in result.stderr, result.stderr
    assert "hostfactors.NoSuchFactor" in result.stderr, result.stderr


@pytest.mark.skipif(not _has_auth(), reason="stapel-auth not installed")
def test_host_override_beats_library_factor_registered_later(tmp_path):
    """Order-independence: core boots FIRST (it is first in
    COMMON_INSTALLED_APPS), stapel-auth registers its own ``otp_email``
    afterwards — and the host's declaration still wins, because
    EXTRA_FACTORS entries are pinned.

    Without the pin this is exactly the trap the product hit: the override
    had to be re-registered from an app listed *below* stapel_auth, and
    moving that app up silently made the override decorative.
    """
    result = _boot(
        tmp_path,
        CORE_APPS + ["stapel_auth"],
        {"EXTRA_FACTORS": ["hostfactors.HostEmailFactor"]},
        """
        from stapel_core.verification import factor_registry
        live = factor_registry.get("otp_email")
        assert getattr(live, "marker", None) == "host", type(live)
        assert factor_registry.pinned_names() == ["otp_email"]
        # The library's other factors are untouched.
        assert "totp" in factor_registry.names(), factor_registry.names()
        print("OVERRIDE_WINS_OK")
        """,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OVERRIDE_WINS_OK" in result.stdout


def test_pin_only_shields_the_declared_id():
    """In-process: pinning is per-id, and a second host declaration re-pins
    (the pre-0.16.1 app-layer call site keeps working)."""
    from stapel_core.verification import VerificationFactor, factor_registry

    class LibFactor(VerificationFactor):
        id = "otp_x"
        strength = "strong"

        def verify(self, user, challenge, payload):  # pragma: no cover
            return False

    class HostFactor(LibFactor):
        strength = "weak"

    class OtherLibFactor(VerificationFactor):
        id = "otp_y"

        def verify(self, user, challenge, payload):  # pragma: no cover
            return False

    factor_registry.clear()
    try:
        factor_registry.register(HostFactor(), pin=True)
        factor_registry.register(LibFactor())          # library, loses
        factor_registry.register(OtherLibFactor())     # unrelated id, lands
        assert factor_registry.get("otp_x").strength == "weak"
        assert factor_registry.get("otp_y").id == "otp_y"
        # A second host declaration re-pins.
        factor_registry.register(LibFactor(), pin=True)
        assert factor_registry.get("otp_x").strength == "strong"
    finally:
        factor_registry.clear()
