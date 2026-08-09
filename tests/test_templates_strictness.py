"""A missing template variable must not render as an empty string in silence."""
import pytest
from django.template import Context, Engine
from django.test import override_settings

from stapel_core.templates import (
    MISSING,
    MissingTemplateVariables,
    assert_no_missing_variables,
    check_missing_variables_are_silent,
    is_strict,
    missing_variables,
    strict_template_variables,
)

DEFAULT_TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]


def _render(source, context, string_if_invalid=""):
    engine = Engine(dirs=[], app_dirs=False, string_if_invalid=string_if_invalid)
    return engine.from_string(source).render(Context(context))


def test_django_default_ships_the_hole():
    """The behaviour being closed, asserted so the fix has a baseline."""
    assert _render("code: [{{ code }}]", {}) == "code: []"


def test_the_sentinel_names_the_variable():
    out = _render("code: {{ code }}", {}, string_if_invalid=MISSING)
    assert missing_variables(out) == ["code"]


def test_present_variables_are_untouched():
    out = _render("code: {{ code }}", {"code": "1234"}, string_if_invalid=MISSING)
    assert out == "code: 1234"
    assert missing_variables(out) == []


def test_if_guard_is_untouched_by_the_sentinel():
    """The reason usually given for not doing this is wrong, and it matters.

    ``IfNode`` catches ``VariableDoesNotExist`` itself and never consults
    ``string_if_invalid``, so a guard on a missing variable is simply false —
    it does not become truthy, and no branch fires that should not. What DOES
    change under a non-empty string_if_invalid is the ``default`` filter, one
    test below.
    """
    out = _render("{% if role %}yes{% else %}no{% endif %}", {}, string_if_invalid=MISSING)
    assert out == "no"
    assert missing_variables(out) == []


def test_default_filter_is_bypassed_and_that_is_why_the_marker_is_a_string():
    """Django returns string_if_invalid for a failed lookup BEFORE the filter
    chain runs, so ``default`` never fires. A raising sentinel would blow up
    where nothing is wrong; a string marker leaves the call to an assertion."""
    out = _render('{{ colour|default:"#fff" }}', {}, string_if_invalid=MISSING)
    assert missing_variables(out) == ["colour"]


def test_assertion_names_every_variable_and_where():
    out = _render("{{ a }}{{ b }}", {}, string_if_invalid=MISSING)
    with pytest.raises(MissingTemplateVariables) as exc:
        assert_no_missing_variables(out, context="mail/otp.html")
    assert "'a'" in str(exc.value) and "'b'" in str(exc.value)
    assert "mail/otp.html" in str(exc.value)


def test_assertion_is_quiet_on_a_complete_render():
    assert_no_missing_variables(_render("{{ a }}", {"a": 1}, string_if_invalid=MISSING))


def test_strict_template_variables_copies_and_marks():
    strictened = strict_template_variables(DEFAULT_TEMPLATES)
    assert is_strict(strictened[0])
    assert not is_strict(DEFAULT_TEMPLATES[0]), "input must not be mutated"
    assert strictened[0]["OPTIONS"]["context_processors"] == []


def test_jinja_engines_are_left_alone():
    jinja = [{"BACKEND": "django.template.backends.jinja2.Jinja2", "OPTIONS": {}}]
    assert strict_template_variables(jinja) == jinja


def test_marker_carries_the_percent_s():
    """Without %s Django never formats the value and the marker cannot name
    the variable — the one way this mechanism fails quiet."""
    assert "%s" in MISSING


def test_check_is_quiet_when_the_marker_is_on():
    with override_settings(DEBUG=True, TEMPLATES=strict_template_variables(DEFAULT_TEMPLATES)):
        assert check_missing_variables_are_silent(None) == []


def test_check_warns_under_debug_when_silent():
    with override_settings(DEBUG=True, TEMPLATES=DEFAULT_TEMPLATES):
        findings = check_missing_variables_are_silent(None)
    assert [f.id for f in findings] == ["stapel_core.templates.W001"]


def test_check_is_quiet_in_production():
    """A dev-loop nudge, not a deploy blocker: the marker is not right for
    every deployment, and the durable gate is the contract test."""
    with override_settings(DEBUG=False, TEMPLATES=DEFAULT_TEMPLATES):
        assert check_missing_variables_are_silent(None) == []
