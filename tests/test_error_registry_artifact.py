"""Core's own ``docs/errors.json`` — the registry half of its i18n contract.

Core owns the 41 cross-cutting error keys (COMMON_ERRORS + the verification
step-up family + captcha/network) and ships their ru/es catalogs in
``django/translations/`` — but until this artifact existed it published no
registry export at all. A consumer that pairs each package's registry with its
catalogs therefore skipped core entirely: 41 translated keys reaching nobody,
in any locale (the ``registry: null`` special-case downstream generators had
to carry). Same failure shape as gdpr pre-0.4.1, closed the same way — the
package ships both halves.

The committed file must be exactly what ``generate_error_keys`` emits from the
core-only registry. Regenerate after adding/changing a core error key:

    STAPEL_REGEN_ERROR_KEYS=1 python -m pytest \
        tests/test_error_registry_artifact.py::test_error_keys_have_no_drift

then commit ``docs/errors.json``. Without the env var the same test is the CI
drift gate.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from stapel_core.django.api.errors import REMEDIATION_VOCAB
from stapel_core.i18n.catalogs import load_catalog_file

REPO = Path(__file__).resolve().parent.parent
ERRORS_JSON = REPO / "docs" / "errors.json"
TRANSLATIONS = REPO / "django" / "translations"
TARGET_LANGUAGES = ("ru", "es")
OWNER = "stapel_core"

#: Emission runs in a fresh interpreter: the error registry is process-global
#: and other tests register probe keys into it, so an in-process regen would
#: depend on test order. The artifact's config is the core-only instance.
_DRIVER = """
import sys
from django.conf import settings
settings.configure(
    DEBUG=True, SECRET_KEY="x" * 40, USE_TZ=True,
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                           "NAME": ":memory:"}},
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "stapel_core.django.apps.CommonDjangoConfig",
    ],
)
import django
django.setup()
from stapel_core.django.management.commands.generate_error_keys import Command
Command().handle(out=sys.argv[1])
"""


def _generate(out: Path) -> None:
    subprocess.run(
        [sys.executable, "-c", _DRIVER, str(out)],
        check=True, capture_output=True, cwd=str(REPO / "tests"),
    )


def test_error_keys_have_no_drift(tmp_path):
    if os.environ.get("STAPEL_REGEN_ERROR_KEYS"):
        _generate(ERRORS_JSON)
        return

    out = tmp_path / "errors.json"
    _generate(out)
    assert ERRORS_JSON.read_bytes() == out.read_bytes(), (
        "errors.json drifted — run "
        "STAPEL_REGEN_ERROR_KEYS=1 pytest tests/test_error_registry_artifact.py "
        "and commit docs/errors.json"
    )


def test_committed_artifact_shape():
    entries = json.loads(ERRORS_JSON.read_text())
    assert isinstance(entries, list) and entries
    codes = [e["code"] for e in entries]
    assert codes == sorted(codes)
    assert len(codes) == len(set(codes))
    for e in entries:
        assert set(e) == {"code", "status", "params", "remediation", "en", "owner"}
        assert e["status"] == int(e["code"].split(".")[1])
        assert e["remediation"] in REMEDIATION_VOCAB
        assert e["owner"] == OWNER  # a core-only instance declares core's keys


def test_registry_declares_every_key_the_catalogs_translate():
    """Catalog → registry: a translated key absent from the export is unreachable."""
    declared = {e["code"] for e in json.loads(ERRORS_JSON.read_text())}
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        undeclared = sorted(k for k in catalog if k not in declared)
        assert not undeclared, (
            f"errors.{lang}.json translates {len(undeclared)} code(s) absent "
            f"from docs/errors.json: {undeclared[:8]}"
        )


def test_catalogs_translate_every_declared_key():
    """Registry → catalog: a declared code must not lack its translation."""
    declared = {e["code"] for e in json.loads(ERRORS_JSON.read_text())}
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        missing = sorted(k for k in declared if k not in catalog)
        assert not missing, (
            f"docs/errors.json declares {len(missing)} code(s) the {lang} "
            f"catalog does not carry: {missing[:8]}"
        )
