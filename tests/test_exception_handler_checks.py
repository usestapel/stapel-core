"""System checks (tag ``stapel_error_envelope``) — a deployment whose
effective ``EXCEPTION_HANDLER`` never reaches core's.

The four cases the check has to get right, in the order they matter: a
settings module without the key at all (the real hole, found in a library's
own test settings while adopting 0.61.0), core's shipped default, a host
wrapper that delegates, and a foreign handler.
"""
import functools

from django.test import override_settings

from stapel_core.django.api import errors
from stapel_core.django.api.errors import stapel_exception_handler
from stapel_core.django.exception_handler_checks import (
    CORE_HANDLER_PATH,
    DELEGATION_ATTR,
    E001_HANDLER_UNUSABLE,
    W001_HANDLER_BYPASSED,
    check_exception_handler_wired,
    reaches_core_handler,
)

BASE = {
    "DEFAULT_PERMISSION_CLASSES": [],
}
WITH_CORE = dict(BASE, EXCEPTION_HANDLER=CORE_HANDLER_PATH)


def _ids(findings):
    return [f.id for f in findings]


# --- the handlers a host could plausibly configure ------------------------


def wrapping_handler(exc, context):
    """The ordinary wrapper: names core's handler in its own body."""
    response = stapel_exception_handler(exc, context)
    if response is not None:
        response["X-Refusal"] = "1"
    return response


def module_attribute_handler(exc, context):
    """The same delegation reached through a module-level import of the
    package — ``errors`` is the only name bound as a global here."""
    return errors.stapel_exception_handler(exc, context)


def deferred_import_handler(exc, context):
    """The import written inside the body — what a wrapper does to dodge
    app-loading order. Nothing is bound until the call runs."""
    from stapel_core.django.api.errors import stapel_exception_handler as delegate

    return delegate(exc, context)


def import_string_handler(exc, context):
    """A wrapper that resolves the delegate at call time by dotted path."""
    from django.utils.module_loading import import_string

    delegate = import_string("stapel_core.django.api.errors.stapel_exception_handler")
    return delegate(exc, context)


@functools.wraps(stapel_exception_handler)
def wrapped_handler(exc, context):
    """``functools.wraps`` — nothing in the body names the delegate."""
    return _DELEGATES[0](exc, context)


_DELEGATES = [stapel_exception_handler]


def outer_wrapper(exc, context):
    """Two levels: a wrapper around :func:`wrapping_handler`."""
    return wrapping_handler(exc, context)


def computed_handler(exc, context):
    """Delegation no reader of this body can see — the declared case."""
    return _DELEGATES[0](exc, context)


computed_handler.stapel_delegates_to_exception_handler = True


def undeclared_computed_handler(exc, context):
    """The same computed delegation, undeclared — the known blind spot."""
    return _DELEGATES[0](exc, context)


def foreign_handler(exc, context):
    """A host's own handler that answers by itself. DRF's own default has
    exactly this shape, and this is the response shape the check reports."""
    from rest_framework.views import exception_handler

    return exception_handler(exc, context)


def stapel_exception_handler_lookalike(exc, context):
    """Named like core's, delegates to nothing. A name-based check passes
    this; this one must not."""
    return None


# --- the four required cases ---------------------------------------------


@override_settings(REST_FRAMEWORK=BASE)
def test_settings_without_the_key_is_caught():
    """The real hole: a project writes its own REST_FRAMEWORK dict, DRF's own
    exception_handler takes over, and every refusal loses the envelope."""
    findings = check_exception_handler_wired()
    assert _ids(findings) == [W001_HANDLER_BYPASSED]
    assert "localizable_error" in findings[0].msg
    assert CORE_HANDLER_PATH in findings[0].hint


@override_settings(REST_FRAMEWORK=WITH_CORE)
def test_core_default_passes():
    assert check_exception_handler_wired() == []


@override_settings(REST_FRAMEWORK=dict(
    BASE, EXCEPTION_HANDLER="tests.test_exception_handler_checks.wrapping_handler"))
def test_wrapper_that_delegates_passes():
    assert check_exception_handler_wired() == []


@override_settings(REST_FRAMEWORK=dict(
    BASE, EXCEPTION_HANDLER="tests.test_exception_handler_checks.foreign_handler"))
def test_foreign_handler_is_caught():
    findings = check_exception_handler_wired()
    assert _ids(findings) == [W001_HANDLER_BYPASSED]
    assert "foreign_handler" in findings[0].msg


# --- the effective setting, not the module default ------------------------


@override_settings(REST_FRAMEWORK=BASE)
def test_reads_the_effective_setting_not_cores_preset():
    """``stapel_core.django.settings`` carries the key; this deployment does
    not. The finding must follow the deployment."""
    from django.conf import settings

    assert "EXCEPTION_HANDLER" not in settings.REST_FRAMEWORK
    assert _ids(check_exception_handler_wired()) == [W001_HANDLER_BYPASSED]


# --- unusable handlers ----------------------------------------------------


@override_settings(REST_FRAMEWORK=dict(
    BASE, EXCEPTION_HANDLER="tests.test_exception_handler_checks.no_such_handler"))
def test_unimportable_handler_is_an_error():
    findings = check_exception_handler_wired()
    assert _ids(findings) == [E001_HANDLER_UNUSABLE]


@override_settings(REST_FRAMEWORK=dict(BASE, EXCEPTION_HANDLER=42))
def test_non_callable_handler_is_an_error():
    findings = check_exception_handler_wired()
    assert _ids(findings) == [E001_HANDLER_UNUSABLE]


# --- how a wrapper is recognised (the honest part) ------------------------


def test_recognises_delegation_through_a_module_attribute():
    assert reaches_core_handler(module_attribute_handler)


def test_recognises_delegation_by_dotted_path_constant():
    assert reaches_core_handler(import_string_handler)


def test_recognises_an_import_deferred_into_the_body():
    assert reaches_core_handler(deferred_import_handler)


def test_recognises_functools_wraps():
    assert reaches_core_handler(wrapped_handler)


def test_recognises_functools_partial():
    assert reaches_core_handler(functools.partial(wrapping_handler))


def test_recognises_two_levels_of_wrapper():
    assert reaches_core_handler(outer_wrapper)


def test_recognises_a_callable_object():
    class Handler:
        def __call__(self, exc, context):
            return stapel_exception_handler(exc, context)

    assert reaches_core_handler(Handler())


def test_declared_delegation_is_accepted():
    """The escape for a wrapper whose delegate is computed — a claim the
    author makes, greppable by attribute name."""
    assert getattr(computed_handler, DELEGATION_ATTR) is True
    assert reaches_core_handler(computed_handler)


def test_a_name_is_never_evidence():
    """The weak check this deliberately is not: same name, no delegation."""
    assert not reaches_core_handler(stapel_exception_handler_lookalike)


def test_undeclared_computed_delegation_is_reported():
    """Stated, not hidden: a wrapper that computes its delegate and does not
    declare it reads as a foreign handler. That is the known limit, and the
    attribute above is its answer."""
    assert not reaches_core_handler(undeclared_computed_handler)
