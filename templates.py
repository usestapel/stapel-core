"""Make a missing template variable visible in tests (tag ``stapel_templates``).

The problem this closes
-----------------------
Django's default is ``string_if_invalid = ''``: a template that reads a
variable the caller never passed renders an empty string and carries on. That
is a reasonable default for a page — and silent data loss for anything
generated once and sent away. The measured case is email. A library renames the
context variable behind ``{{ code }}`` in a patch release; every one of its own
tests stays green, because they render with the library's own context; the
host's overridden template still asks for ``code``; the OTP mail ships with a
blank space where the code was. HTTP 200, no exception, no log line, and the
person cannot log in.

The shape: a sentinel plus an assertion, not a crash
----------------------------------------------------
``string_if_invalid`` is substituted with a recognisable marker, and a test
renders each template against its declared context and asserts the marker is
not in the output. That catches exactly the failure and nothing else.

The alternative — making the engine *raise* — was built first and rejected,
for a reason worth recording because it is not the reason usually given:

* It is often said that raising breaks ``{% if var %}``. It does not:
  ``IfNode`` catches ``VariableDoesNotExist`` itself and never consults
  ``string_if_invalid``, so a guard on a missing variable is simply false. The
  test ``test_if_guard_is_untouched_by_the_sentinel`` pins that.
* What raising **does** break is ``{{ x|default:"y" }}``. When
  ``string_if_invalid`` is non-empty Django returns it for a failed lookup
  *before* the filter chain runs, so the ``default`` never fires. Under a
  raising sentinel that is an exception where nothing is wrong; under a
  string sentinel it is a visible marker in the output, which an assertion can
  weigh in context. Stock Django templates — the admin above all — rely on
  this, and ``string_if_invalid`` is engine-wide, not per-template.

So the marker is a plain string, it names the variable, and the failure is an
assertion in a test rather than a behaviour change at render time.

Where it belongs, and where it does not
---------------------------------------
* **Test settings** — yes. This is the home.
* **Dev settings** — the check below tells you it is off; enabling it is a
  one-liner. Left to the project, because a marker in a rendered dev page is a
  cosmetic surprise and the durable gate is not here anyway.
* **Production** — no. This package must not change how a host's mail or pages
  render as a side effect of an upgrade.
* **The real gate** lives outside this file: a contract artifact the host
  asserts against in CI (``docs/templates.json`` +
  ``stapel_tools.template_contract``). The sentinel catches the variable a test
  happened to exercise; the contract catches the one it did not.

Usage — a test settings module::

    from stapel_core.templates import strict_template_variables

    TEMPLATES = strict_template_variables(TEMPLATES)

and in a test that renders::

    from stapel_core.templates import assert_no_missing_variables

    assert_no_missing_variables(html)
"""
from __future__ import annotations

import re

from django.core import checks

W001_SILENT_MISSING_VARIABLES = "stapel_core.templates.W001"

#: The marker substituted for a variable that is not in the context. ``%s`` is
#: mandatory — without it Django never formats the value and the marker cannot
#: name the variable. Chosen to be findable in rendered HTML and impossible to
#: mistake for content.
MISSING = "!!MISSING-TEMPLATE-VAR:%s!!"

_MISSING_RE = re.compile(r"!!MISSING-TEMPLATE-VAR:(.*?)!!")

_HINT = (
    "In your TEST settings: "
    "TEMPLATES = strict_template_variables(TEMPLATES) "
    "(stapel_core.templates), then assert_no_missing_variables"
    "(rendered) in tests that render. Leave production alone — the durable gate "
    "is a template contract asserted in CI, not a rendering flag."
)


class MissingTemplateVariables(AssertionError):
    """Rendered output contains the missing-variable marker.

    An ``AssertionError`` on purpose: this is a test failure, in the test that
    rendered the thing, not a runtime exception somewhere downstream.
    """


def strict_template_variables(templates: list[dict], *, backend: str | None = None) -> list[dict]:
    """Return ``TEMPLATES`` with the missing-variable marker switched on.

    Copies rather than mutates, so the settings module that assigns the result
    is the only thing that changes. ``backend`` limits the change to one engine
    by its ``BACKEND`` or ``NAME``; by default every DjangoTemplates engine
    gets the marker (a Jinja2 engine has no ``string_if_invalid`` and is left
    alone).
    """
    out = []
    for engine in templates:
        engine_backend = engine.get("BACKEND", "")
        wanted = backend in (None, engine_backend, engine.get("NAME"))
        if not wanted or "django.template.backends.django" not in engine_backend:
            out.append(engine)
            continue
        copied = dict(engine)
        options = dict(copied.get("OPTIONS") or {})
        options["string_if_invalid"] = MISSING
        copied["OPTIONS"] = options
        out.append(copied)
    return out


def is_strict(engine: dict) -> bool:
    return (engine.get("OPTIONS") or {}).get("string_if_invalid") == MISSING


def missing_variables(rendered: str) -> list[str]:
    """Names of the variables the render could not resolve, in order."""
    return _MISSING_RE.findall(rendered)


def assert_no_missing_variables(rendered: str, *, context: str = "") -> None:
    """Fail if ``rendered`` carries the marker, naming every variable.

    ``context`` is anything that helps place the failure — a template name, a
    notification type. The message says what the default would have done,
    because "an empty string" is the part that makes this class of bug survive
    review.
    """
    found = missing_variables(rendered)
    if not found:
        return
    where = f" while rendering {context}" if context else ""
    raise MissingTemplateVariables(
        f"template variable(s) {sorted(set(found))} were not in the render "
        f"context{where}. With Django's default string_if_invalid='' each of "
        "them would have rendered as an empty string and the result would have "
        "been sent. If a variable is genuinely optional, guard it with "
        "{% if %} — note that a `default` filter does NOT apply once "
        "string_if_invalid is set."
    )


@checks.register("stapel_templates")
def check_missing_variables_are_silent(app_configs, **kwargs):
    """W001 under DEBUG: this engine renders unknown variables as ''.

    W and not E, and DEBUG-only, deliberately: the marker is not correct for
    every deployment (see the module docstring), so this states what the
    current configuration will do, where somebody is looking —
    ``manage.py runserver`` runs the checks on every boot.
    """
    from django.conf import settings

    if not getattr(settings, "DEBUG", False):
        return []
    findings = []
    for index, engine in enumerate(getattr(settings, "TEMPLATES", []) or []):
        backend = engine.get("BACKEND", "")
        if "django.template.backends.django" not in backend:
            continue
        if (engine.get("OPTIONS") or {}).get("string_if_invalid"):
            continue
        findings.append(
            checks.Warning(
                "TEMPLATES[%d] renders a missing variable as an empty string "
                "(Django's default string_if_invalid). A template that reads a "
                "variable nobody passes produces a blank space and no error — "
                "which is how a renamed context variable ships an email with a "
                "hole in it." % index,
                hint=_HINT,
                id=W001_SILENT_MISSING_VARIABLES,
            )
        )
    return findings
