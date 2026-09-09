from dataclasses import dataclass

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import override_settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    ErrorDetail,
    NotAuthenticated,
    Throttled,
)
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate
from stapel_core.django.api.errors import (
    COMMON_ERRORS,
    ERR_400_BAD_REQUEST,
    ERR_401_UNAUTHORIZED,
    ERR_403_FORBIDDEN,
    ERR_404_NOT_FOUND,
    ERR_405_METHOD_NOT_ALLOWED,
    ERR_429_RATE_LIMIT,
    ERR_429_TOO_MANY_REQUESTS,
    ERR_500_INTERNAL,
    REMEDIATION_VOCAB,
    StapelError,
    StapelErrorResponse,
    StapelResponse,
    StapelServiceError,
    StapelValidationError,
    build_error_registry,
    default_remediation,
    error_400_bad_request,
    error_401_unauthorized,
    error_403_forbidden,
    error_404_not_found,
    error_429_rate_limit,
    error_500_internal,
    format_duration,
    register_service_errors,
    stapel_exception_handler,
)
from stapel_core.django.api.serializers import StapelDataclassSerializer

_factory = APIRequestFactory()


def _ctx():
    return {"request": _factory.get("/"), "view": None}


class _AuthedUser:
    """Enough of a user for IsAuthenticated — no database needed."""

    is_authenticated = True
    is_active = True
    is_anonymous = False
    pk = 1


# ---------------------------------------------------------------------------
# StapelErrorResponse
# ---------------------------------------------------------------------------


class TestStapelErrorResponse:
    def test_status_code(self):
        resp = StapelErrorResponse(404, ERR_404_NOT_FOUND)
        assert resp.status_code == 404

    def test_body_has_required_keys(self):
        resp = StapelErrorResponse(400, ERR_400_BAD_REQUEST)
        assert "localizable_error" in resp.data
        assert "error" in resp.data
        assert "params" in resp.data
        assert "error_language" in resp.data

    def test_localizable_error_matches_key(self):
        resp = StapelErrorResponse(400, ERR_400_BAD_REQUEST)
        assert resp.data["localizable_error"] == ERR_400_BAD_REQUEST

    def test_error_message_populated_from_registry(self):
        resp = StapelErrorResponse(404, ERR_404_NOT_FOUND)
        assert resp.data["error"] != ""
        assert resp.data["error"] != ERR_404_NOT_FOUND  # should be the English text

    def test_params_passed_through(self):
        resp = StapelErrorResponse(
            429,
            ERR_429_RATE_LIMIT,
            params={
                "retry_after": 60,
                "retry_after_minutes": 1,
                "retry_after_display": "1:00",
            },
        )
        assert resp.data["params"]["retry_after"] == 60

    def test_unknown_key_uses_key_as_error(self):
        resp = StapelErrorResponse(400, "error.custom.unknown.key")
        assert resp.data["localizable_error"] == "error.custom.unknown.key"

    def test_template_formatting(self):
        resp = StapelErrorResponse(
            400,
            "error.400.field.max_length",
            params={
                "field": "name",
                "max_length": 100,
            },
        )
        assert "100" in resp.data["error"] or "name" in resp.data["error"]

    def test_params_default_to_empty_dict(self):
        resp = StapelErrorResponse(400, ERR_400_BAD_REQUEST)
        assert resp.data["params"] == {}


# ---------------------------------------------------------------------------
# Common error helpers
# ---------------------------------------------------------------------------


class TestCommonErrorHelpers:
    def test_error_400(self):
        assert error_400_bad_request().status_code == 400

    def test_error_401(self):
        assert error_401_unauthorized().status_code == 401

    def test_error_403(self):
        assert error_403_forbidden().status_code == 403

    def test_error_404(self):
        assert error_404_not_found().status_code == 404

    def test_error_500(self):
        assert error_500_internal().status_code == 500


# ---------------------------------------------------------------------------
# StapelResponse
# ---------------------------------------------------------------------------


class TestStapelResponse:
    def _make_serializer(self):
        @dataclass
        class MyDto:
            """Test DTO.

            Attributes:
                value: A value. Example: 42
                name: A name. Example: Alice
            """

            value: int
            name: str

        class MySerializer(StapelDataclassSerializer):
            class Meta:
                dataclass = MyDto

        return MySerializer

    def test_auto_calls_data_on_serializer(self):
        from dataclasses import dataclass as dc

        @dc
        class MyDto2:
            """DTO.

            Attributes:
                value: V. Example: 1
                name: N. Example: x
            """

            value: int
            name: str

        class Ser2(StapelDataclassSerializer):
            class Meta:
                dataclass = MyDto2

        inst = MyDto2(value=99, name="test")
        resp = StapelResponse(Ser2(inst))
        assert resp.data == {"value": 99, "name": "test"}

    def test_accepts_dict_data_directly(self):
        resp = StapelResponse({"key": "val"})
        assert resp.data == {"key": "val"}

    def test_empty_response_204(self):
        resp = StapelResponse(status=204)
        assert resp.status_code == 204
        assert resp.data is None

    def test_default_status_200(self):
        resp = StapelResponse({"x": 1})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    def test_zero(self):
        assert format_duration(0) == "0:00"

    def test_none(self):
        assert format_duration(None) == "0:00"

    def test_under_one_minute(self):
        assert format_duration(45) == "0:45"

    def test_one_minute(self):
        assert format_duration(60) == "1:00"

    def test_one_minute_thirty(self):
        assert format_duration(90) == "1:30"

    def test_two_minutes(self):
        assert format_duration(120) == "2:00"

    def test_one_hour(self):
        assert format_duration(3600) == "1:00:00"

    def test_one_hour_one_minute_one_second(self):
        assert format_duration(3661) == "1:01:01"

    def test_two_hours(self):
        assert format_duration(7200) == "2:00:00"

    def test_float_truncated(self):
        assert format_duration(90.9) == "1:30"


# ---------------------------------------------------------------------------
# error_429_rate_limit
# ---------------------------------------------------------------------------


class TestError429RateLimit:
    def test_returns_429_status(self):
        resp = error_429_rate_limit(60)
        assert resp.status_code == 429

    def test_localizable_key(self):
        resp = error_429_rate_limit(60)
        assert resp.data["localizable_error"] == ERR_429_RATE_LIMIT

    def test_params_retry_after(self):
        resp = error_429_rate_limit(120)
        assert resp.data["params"]["retry_after"] == 120

    def test_params_retry_after_minutes_rounds_up(self):
        resp = error_429_rate_limit(61)  # just over 1 minute
        assert resp.data["params"]["retry_after_minutes"] == 2

    def test_params_retry_after_minutes_minimum_1(self):
        resp = error_429_rate_limit(0)
        assert resp.data["params"]["retry_after_minutes"] >= 1

    def test_params_retry_after_display(self):
        resp = error_429_rate_limit(90)
        assert resp.data["params"]["retry_after_display"] == "1:30"


# ---------------------------------------------------------------------------
# stapel_exception_handler
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_sandbox():
    """Register error keys and take them back out again.

    The registry is process-global by design, so a test that adds a key must
    remove it or every later test sees it — including build_error_registry's
    drift gate.
    """
    from stapel_core.django.api import errors as errors_module

    added: list = []

    def _register(mapping, **kwargs):
        added.extend(mapping)
        register_service_errors(mapping, **kwargs)

    yield _register

    for code in added:
        errors_module._GLOBAL_REGISTRY.pop(code, None)
        errors_module._LANGUAGE_REGISTRY.pop(code, None)
        errors_module._OWNER_REGISTRY.pop(code, None)


class TestErrorLanguage:
    """error_language labels the LANGUAGE THE `error` STRING IS IN.

    A client (@stapel/core 0.26.1) compares it to the UI locale with `===`
    and prints `error` verbatim on a match, so a wrong label is the trigger,
    not a cosmetic detail. Until 0.62.0 the field defaulted to the active
    locale at dataclass construction, which labelled the registry's plain
    English templates as the language of whoever asked.
    """

    def test_a_registry_template_is_not_claimed_to_be_the_request_locale(self):
        """The live defect (a client stand, 2026-09-09): a service whose
        active locale is `ru` answered an English registry sentence labelled
        `"error_language": "ru"`. Fails on the code before this release."""
        from django.utils.translation import override

        with override("ru"):
            resp = StapelErrorResponse(404, ERR_404_NOT_FOUND)

        assert resp.data["error"] == "Requested resource not found"  # English
        assert resp.data["error_language"] != "ru"
        assert resp.data["error_language"] == "en"

    def test_a_registry_template_is_the_same_language_in_every_locale(self):
        from django.utils.translation import override

        with override("ru"):
            ru = StapelErrorResponse(400, ERR_400_BAD_REQUEST).data
        with override("en"):
            en = StapelErrorResponse(400, ERR_400_BAD_REQUEST).data
        assert ru["error"] == en["error"]
        assert ru["error_language"] == en["error_language"] == "en"

    def test_the_tier_gettext_really_translates_keeps_the_active_locale(self):
        """str(detail) over DRF's gettext_lazy messages IS in the request's
        locale — the one tier for which the old default was right."""
        from django.utils.translation import override

        with override("ru"):
            exc = DRFValidationError({"name": [ErrorDetail("Too long", code="max_length")]})
            resp = stapel_exception_handler(exc, _ctx())
            assert resp.data["error_language"] == "ru"

    def test_django_validation_tier_keeps_the_active_locale(self):
        from django.utils.translation import override

        with override("ru"):
            exc = DjangoValidationError({"name": ["This field is required."]})
            resp = stapel_exception_handler(exc, _ctx())
            assert resp.data["error_language"] == "ru"

    def test_service_error_tier_is_registry_sourced(self):
        from django.utils.translation import override

        with override("ru"):
            exc = StapelServiceError(403, ERR_403_FORBIDDEN)
            resp = stapel_exception_handler(exc, _ctx())
            assert resp.data["error"] == COMMON_ERRORS[ERR_403_FORBIDDEN]
            assert resp.data["error_language"] == "en"

    def test_drf_refusal_tier_is_registry_sourced(self):
        """Tier 4 re-dresses DRF's body through the registry, so the sentence
        the client sees is core's English one and must be labelled as such."""
        from django.utils.translation import override

        with override("ru"):
            resp = stapel_exception_handler(NotAuthenticated(), _ctx())
        assert resp.data["localizable_error"] == ERR_401_UNAUTHORIZED
        assert resp.data["error_language"] == "en"

    def test_an_unregistered_key_claims_no_language(self):
        """`error` is then the key itself, which is not a sentence at all."""
        resp = StapelErrorResponse(400, "error.400.nobody.registered.this")
        assert resp.data["error"] == "error.400.nobody.registered.this"
        assert resp.data["error_language"] == ""

    def test_a_host_declares_the_language_it_wrote_its_templates_in(
        self, registry_sandbox
    ):
        from django.utils.translation import override

        registry_sandbox({"error.400.po_russki": "Неверный запрос"}, language="ru")
        with override("en"):
            resp = StapelErrorResponse(400, "error.400.po_russki")
        assert resp.data["error_language"] == "ru"

    def test_an_undeclared_host_key_claims_nothing_by_default(
        self, registry_sandbox
    ):
        """Fail closed where the library cannot know: no declaration, no claim,
        so the client translates from localizable_error+params."""
        registry_sandbox({"error.400.undeclared": "Something a host wrote"})
        resp = StapelErrorResponse(400, "error.400.undeclared")
        assert resp.data["error_language"] == ""

    @override_settings(STAPEL_CORE={"ERROR_REGISTRY_LANGUAGE": "en"})
    def test_the_deployment_can_say_what_undeclared_means(self, registry_sandbox):
        """Shape (A): assume undeclared templates are English. A declaration
        still wins over the assumption."""
        registry_sandbox({"error.400.undeclared_a": "Something a host wrote"})
        registry_sandbox({"error.400.declared_a": "Неверный запрос"}, language="ru")
        assert StapelErrorResponse(400, "error.400.undeclared_a").data[
            "error_language"
        ] == "en"
        assert StapelErrorResponse(400, "error.400.declared_a").data[
            "error_language"
        ] == "ru"

    @override_settings(STAPEL_CORE={"ERROR_REGISTRY_LANGUAGE": ""})
    def test_the_deployment_can_fail_closed_for_everything(self, registry_sandbox):
        """Shape (B): no client ever prints a registry sentence verbatim —
        core's own keys and declared host keys included."""
        registry_sandbox({"error.400.declared_b": "Неверный запрос"}, language="ru")
        assert StapelErrorResponse(404, ERR_404_NOT_FOUND).data["error_language"] == ""
        assert StapelErrorResponse(400, "error.400.declared_b").data[
            "error_language"
        ] == ""

    def test_a_construction_site_that_says_nothing_claims_nothing(self):
        """The root cause was a request-derived default on the dataclass."""
        from django.utils.translation import override

        with override("ru"):
            assert StapelError(localizable_error="x", error="y").error_language == ""


class TestIronExceptionHandler:
    # StapelServiceError
    def test_iron_service_error_correct_status(self):
        exc = StapelServiceError(403, ERR_403_FORBIDDEN)
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 403
        assert resp.data["localizable_error"] == ERR_403_FORBIDDEN

    def test_iron_service_error_with_params(self):
        exc = StapelServiceError(
            429,
            ERR_429_RATE_LIMIT,
            params={
                "retry_after": 60,
                "retry_after_minutes": 1,
                "retry_after_display": "1:00",
            },
        )
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 429
        assert resp.data["params"]["retry_after"] == 60

    def test_iron_service_error_500(self):
        exc = StapelServiceError(500, ERR_500_INTERNAL)
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 500

    # StapelValidationError
    def test_iron_validation_error_returns_400(self):
        exc = StapelValidationError(ERR_400_BAD_REQUEST)
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 400
        assert resp.data["localizable_error"] == ERR_400_BAD_REQUEST

    def test_iron_validation_error_with_params(self):
        exc = StapelValidationError(
            "error.400.field.max_length", params={"field": "bio", "max_length": 200}
        )
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 400
        assert resp.data["params"]["field"] == "bio"

    # DRF field-level errors
    def test_drf_required_field_error(self):
        exc = DRFValidationError(
            {"email": [ErrorDetail("This field is required.", code="required")]}
        )
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 400
        assert resp.data["localizable_error"] == "error.400.field.required"
        assert resp.data["params"]["field"] == "email"

    def test_drf_max_length_field_error(self):
        exc = DRFValidationError({"name": [ErrorDetail("Too long", code="max_length")]})
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.data["localizable_error"] == "error.400.field.max_length"
        assert resp.data["params"]["field"] == "name"
        # No serializer attached (a bare DRFValidationError, as any plain
        # rest_framework.serializers.Serializer would raise) -> no limit in
        # params, exactly the pre-existing behavior.
        assert "max_length" not in resp.data["params"]

    def test_drf_field_error_with_attached_serializer_carries_limit(self):
        """StapelDataclassSerializer.is_valid() attaches itself to the raised
        exception so the handler can read the field's declared limit — a
        frontend i18n consumer needs the number (`max_length: 5`), not just
        which field and which kind of error."""

        @dataclass
        class _LimitedDoc:
            name: str
            age: int

        class _LimitedSerializer(StapelDataclassSerializer):
            class Meta:
                dataclass = _LimitedDoc
                extra_kwargs = {
                    "name": {"max_length": 5},
                    "age": {"max_value": 10, "min_value": 0},
                }

        serializer = _LimitedSerializer(data={"name": "toolongname", "age": 3})
        with pytest.raises(DRFValidationError) as excinfo:
            serializer.is_valid(raise_exception=True)

        resp = stapel_exception_handler(excinfo.value, _ctx())
        assert resp.data["localizable_error"] == "error.400.field.max_length"
        assert resp.data["params"]["field"] == "name"
        assert resp.data["params"]["max_length"] == 5

    def test_drf_min_max_value_field_errors_carry_limits(self):
        @dataclass
        class _RangedDoc:
            age: int

        class _RangedSerializer(StapelDataclassSerializer):
            class Meta:
                dataclass = _RangedDoc
                extra_kwargs = {"age": {"max_value": 10, "min_value": 0}}

        too_high = _RangedSerializer(data={"age": 999})
        with pytest.raises(DRFValidationError) as excinfo:
            too_high.is_valid(raise_exception=True)
        resp = stapel_exception_handler(excinfo.value, _ctx())
        assert resp.data["params"]["max_value"] == 10

        too_low = _RangedSerializer(data={"age": -5})
        with pytest.raises(DRFValidationError) as excinfo:
            too_low.is_valid(raise_exception=True)
        resp = stapel_exception_handler(excinfo.value, _ctx())
        assert resp.data["params"]["min_value"] == 0

    def test_is_valid_without_raise_exception_behaves_as_before(self):
        @dataclass
        class _PlainDoc:
            name: str

        class _PlainSerializer(StapelDataclassSerializer):
            class Meta:
                dataclass = _PlainDoc

        serializer = _PlainSerializer(data={})
        assert serializer.is_valid() is False
        assert serializer.errors  # populated exactly like stock DRF

    def test_is_valid_true_on_valid_data(self):
        @dataclass
        class _PlainDoc:
            name: str

        class _PlainSerializer(StapelDataclassSerializer):
            class Meta:
                dataclass = _PlainDoc

        serializer = _PlainSerializer(data={"name": "ok"})
        assert serializer.is_valid(raise_exception=True) is True

    def test_drf_non_field_errors(self):
        exc = DRFValidationError([ErrorDetail("Some non-field error", code="invalid")])
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 400

    def test_drf_non_field_errors_dict(self):
        exc = DRFValidationError(
            {"non_field_errors": [ErrorDetail("Password mismatch", code="invalid")]}
        )
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 400

    def test_drf_registered_key_as_string_detail(self):
        exc = DRFValidationError(ERR_404_NOT_FOUND)
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.data["localizable_error"] == ERR_404_NOT_FOUND

    # Django ValidationError
    def test_django_validation_error_dict(self):
        exc = DjangoValidationError({"name": ["This field is required."]})
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 400

    def test_django_validation_error_message(self):
        exc = DjangoValidationError("Something went wrong.")
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 400

    # Unknown exception falls through to DRF default
    def test_unknown_exception_returns_none(self):
        exc = ValueError("not an API error")
        resp = stapel_exception_handler(exc, _ctx())
        assert resp is None


# ---------------------------------------------------------------------------
# StapelValidationError params surviving DRF's own collapse
#
# DRF's Serializer.to_internal_value/run_validation catch a ValidationError
# raised from a field validator, .validate(), or a nested serializer and
# re-raise a *new* ValidationError wrapping the collected errors — every
# re-raise runs the detail through rest_framework.exceptions
# ._get_error_details, which discards anything but the leaf's text and
# `.code`. Before the _StapelErrorCode carrier, `params` never survived that
# trip and only the registered error_key string did (measured in
# stapel-listings 0.22.3: draft_meta_too_large lost max_bytes). All four
# paths below must land on the same params the caller raised with.
# ---------------------------------------------------------------------------


class TestStapelValidationErrorParamsSurviveCollapse:
    CODE = "error.400.draft_meta_too_large"

    def _doc_serializer(self, raise_in):
        """raise_in: 'field_validator' | 'validators_list' | 'validate' — where
        the StapelValidationError is raised inside the serializer."""
        from dataclasses import dataclass

        @dataclass
        class _Doc:
            meta: str

        params = {"max_bytes": 4096}

        if raise_in == "field_validator":

            class _S(StapelDataclassSerializer):
                class Meta:
                    dataclass = _Doc

                def validate_meta(self, value):
                    raise StapelValidationError(
                        TestStapelValidationErrorParamsSurviveCollapse.CODE, params=params
                    )

            return _S

        if raise_in == "validators_list":

            def _validator(value):
                raise StapelValidationError(
                    TestStapelValidationErrorParamsSurviveCollapse.CODE, params=params
                )

            class _S(StapelDataclassSerializer):
                class Meta:
                    dataclass = _Doc
                    extra_kwargs = {"meta": {"validators": [_validator]}}

            return _S

        class _S(StapelDataclassSerializer):
            class Meta:
                dataclass = _Doc

            def validate(self, attrs):
                raise StapelValidationError(
                    TestStapelValidationErrorParamsSurviveCollapse.CODE, params=params
                )

        return _S

    def test_field_validator_method(self):
        serializer_cls = self._doc_serializer("field_validator")
        serializer = serializer_cls(data={"meta": "x"})
        with pytest.raises(DRFValidationError) as excinfo:
            serializer.is_valid(raise_exception=True)

        resp = stapel_exception_handler(excinfo.value, _ctx())
        assert resp.data["localizable_error"] == self.CODE
        assert resp.data["params"]["max_bytes"] == 4096
        assert resp.data["params"]["field"] == "meta"

    def test_field_validators_kwarg(self):
        serializer_cls = self._doc_serializer("validators_list")
        serializer = serializer_cls(data={"meta": "x"})
        with pytest.raises(DRFValidationError) as excinfo:
            serializer.is_valid(raise_exception=True)

        resp = stapel_exception_handler(excinfo.value, _ctx())
        assert resp.data["localizable_error"] == self.CODE
        assert resp.data["params"]["max_bytes"] == 4096

    def test_validate_method(self):
        serializer_cls = self._doc_serializer("validate")
        serializer = serializer_cls(data={"meta": "x"})
        with pytest.raises(DRFValidationError) as excinfo:
            serializer.is_valid(raise_exception=True)

        resp = stapel_exception_handler(excinfo.value, _ctx())
        assert resp.data["localizable_error"] == self.CODE
        assert resp.data["params"]["max_bytes"] == 4096

    def test_nested_serializer(self):
        from rest_framework import serializers as drf_serializers

        inner_cls = self._doc_serializer("field_validator")

        class _Outer(drf_serializers.Serializer):
            inner = inner_cls()

        serializer = _Outer(data={"inner": {"meta": "x"}})
        with pytest.raises(DRFValidationError) as excinfo:
            serializer.is_valid(raise_exception=True)

        resp = stapel_exception_handler(excinfo.value, _ctx())
        assert resp.data["localizable_error"] == self.CODE
        assert resp.data["params"]["max_bytes"] == 4096

    def test_directly_from_a_view(self):
        exc = StapelValidationError(self.CODE, params={"max_bytes": 4096})
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.data["localizable_error"] == self.CODE
        assert resp.data["params"]["max_bytes"] == 4096


# ---------------------------------------------------------------------------
# register_service_errors
# ---------------------------------------------------------------------------


class TestRegisterServiceErrors:
    def test_custom_key_renders_correct_message(self):
        register_service_errors({"error.test.my_custom": "My custom error text"})
        resp = StapelErrorResponse(400, "error.test.my_custom")
        assert resp.data["error"] == "My custom error text"

    def test_custom_key_with_template(self):
        register_service_errors({"error.test.templated": "Value is {val}"})
        resp = StapelErrorResponse(400, "error.test.templated", params={"val": "bad"})
        assert resp.data["error"] == "Value is bad"

    def test_bad_template_params_falls_back_to_template(self):
        register_service_errors({"error.test.broken_template": "Value is {val}"})
        # Missing param — should not raise, falls back to raw template
        resp = StapelErrorResponse(400, "error.test.broken_template")
        assert "Value is" in resp.data["error"]


# ---------------------------------------------------------------------------
# Remediation registry + errors.json artifact projection
# ---------------------------------------------------------------------------


class TestRemediation:
    def test_declared_remediation_wins_over_heuristic(self):
        register_service_errors(
            {"error.409.rem_declared": "x"},
            remediation={"error.409.rem_declared": "reauthenticate"},
        )
        entry = next(
            e for e in build_error_registry() if e["code"] == "error.409.rem_declared"
        )
        # heuristic for 409 is fix_input; the declaration overrides it
        assert entry["remediation"] == "reauthenticate"

    def test_undeclared_key_falls_back_to_heuristic(self):
        register_service_errors({"error.409.rem_heuristic": "x"})
        entry = next(
            e for e in build_error_registry() if e["code"] == "error.409.rem_heuristic"
        )
        assert entry["remediation"] == "fix_input"

    def test_rejects_remediation_for_unknown_key(self):
        import pytest

        with pytest.raises(ValueError, match="unknown error key"):
            register_service_errors(
                {"error.400.rem_a": "a"},
                remediation={"error.400.rem_b": "retry"},
            )

    def test_rejects_invalid_remediation_value(self):
        import pytest

        with pytest.raises(ValueError, match="invalid remediation"):
            register_service_errors(
                {"error.400.rem_bad": "a"},
                remediation={"error.400.rem_bad": "do_something"},
            )

    def test_default_remediation_heuristic(self):
        assert default_remediation("error.401.x", 401, []) == "reauthenticate"
        assert default_remediation("error.500.x", 500, []) == "contact_support"
        assert default_remediation("error.423.x", 423, []) == "wait_and_retry"
        assert default_remediation("error.400.y", 400, ["retry_after"]) == "wait_and_retry"
        assert default_remediation("error.400.step_up_required", 400, []) == "verify"
        assert default_remediation("error.404.not_found", 404, []) == "retry"
        assert default_remediation("error.404.user_x", 404, []) == "fix_input"
        assert default_remediation("error.400.plain", 400, []) == "fix_input"
        assert default_remediation("error.400.qr_expired", 400, []) == "retry"


class TestBuildErrorRegistry:
    def test_shape_sorted_and_complete(self):
        register_service_errors({"error.400.artifact_key": "Bad {field} value"})
        entries = build_error_registry()
        codes = [e["code"] for e in entries]
        assert codes == sorted(codes)
        entry = next(e for e in entries if e["code"] == "error.400.artifact_key")
        assert set(entry) == {"code", "status", "params", "remediation", "en", "owner"}
        assert entry["owner"]  # inferred from the registering caller
        assert entry["status"] == 400
        assert entry["params"] == ["field"]
        assert entry["remediation"] in REMEDIATION_VOCAB
        assert entry["en"] == "Bad {field} value"

    def test_params_deduped_first_seen_order(self):
        register_service_errors({"error.400.dup": "{a} then {b} then {a}"})
        entry = next(
            e for e in build_error_registry() if e["code"] == "error.400.dup"
        )
        assert entry["params"] == ["a", "b"]


# ---------------------------------------------------------------------------
# Tier 4 — the exceptions no view raises
#
# DRF's authenticators raise NotAuthenticated/AuthenticationFailed, its
# permission classes raise PermissionDenied, its dispatch raises
# MethodNotAllowed, get_object_or_404 raises Http404 and throttles raise
# Throttled. None of them ever passes through StapelErrorResponse, so all of
# them used to answer DRF's bare {"detail": ...} — measured on a live stand
# across four unrelated endpoints. They now carry the same envelope every
# other error does, with DRF's status verdict and DRF's headers untouched.
# ---------------------------------------------------------------------------


CHALLENGE = 'Bearer realm="api"'


class _ChallengingAuth(BaseAuthentication):
    """Authenticates nobody, but offers a WWW-Authenticate challenge — the
    shape that makes DRF answer 401 rather than coercing to 403."""

    def authenticate(self, request):
        return None

    def authenticate_header(self, request):
        return CHALLENGE


class _SilentAuth(BaseAuthentication):
    """No challenge to offer, so DRF downgrades an unauthenticated refusal to
    403 (APIView.handle_exception). The envelope must follow that verdict."""

    def authenticate(self, request):
        return None


class _DenyAll(BasePermission):
    def has_permission(self, request, view):
        return False


def _view(**attrs):
    """A GET-only APIView with the given policy attributes."""
    from rest_framework.views import APIView

    body = {"get": lambda self, request: StapelResponse({"ok": True})}
    body.update(attrs)
    return type("_TestView", (APIView,), body).as_view()


class TestDrfExceptionsGetTheEnvelope:
    def test_anonymous_is_401_with_envelope_and_challenge_header(self):
        view = _view(
            authentication_classes=[_ChallengingAuth],
            permission_classes=[IsAuthenticated],
        )
        resp = view(_factory.get("/"))
        assert resp.status_code == 401
        assert resp.data["localizable_error"] == ERR_401_UNAUTHORIZED
        assert resp.data["error"] == "Authentication required"
        assert "params" in resp.data and "error_language" in resp.data
        # Losing this header would leave a client unable to authenticate.
        assert resp["WWW-Authenticate"] == CHALLENGE

    def test_anonymous_without_a_challenge_keeps_drfs_403_verdict(self):
        view = _view(
            authentication_classes=[_SilentAuth],
            permission_classes=[IsAuthenticated],
        )
        resp = view(_factory.get("/"))
        assert resp.status_code == 403
        assert resp.data["localizable_error"] == ERR_403_FORBIDDEN
        assert "WWW-Authenticate" not in resp

    def test_authenticated_but_forbidden_stays_403(self):
        view = _view(
            authentication_classes=[_ChallengingAuth],
            permission_classes=[_DenyAll],
        )
        request = _factory.get("/")
        force_authenticate(request, user=_AuthedUser())
        resp = view(request)
        assert resp.status_code == 403
        assert resp.data["localizable_error"] == ERR_403_FORBIDDEN
        # An authenticated caller is not asked to authenticate again.
        assert "WWW-Authenticate" not in resp

    def test_authentication_failed_is_401_with_challenge(self):
        class _RejectingAuth(BaseAuthentication):
            def authenticate(self, request):
                raise AuthenticationFailed("bad token")

            def authenticate_header(self, request):
                return CHALLENGE

        view = _view(authentication_classes=[_RejectingAuth])
        resp = view(_factory.get("/"))
        assert resp.status_code == 401
        assert resp.data["localizable_error"] == ERR_401_UNAUTHORIZED
        assert resp["WWW-Authenticate"] == CHALLENGE
        assert resp.data["params"]["detail"] == "bad token"

    def test_original_detail_is_kept_in_params(self):
        exc = NotAuthenticated()
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.data["params"]["detail"] == exc.detail

    # --- the three optional ones: NotFound, MethodNotAllowed, Throttled ---

    def test_http404_from_a_view_is_enveloped(self):
        from django.http import Http404

        def _get(self, request):
            raise Http404("no such thing")

        resp = _view(get=_get)(_factory.get("/"))
        assert resp.status_code == 404
        assert resp.data["localizable_error"] == ERR_404_NOT_FOUND

    def test_method_not_allowed_is_enveloped(self):
        resp = _view()(_factory.delete("/"))
        assert resp.status_code == 405
        assert resp.data["localizable_error"] == ERR_405_METHOD_NOT_ALLOWED
        assert 'Method "DELETE" not allowed.' in str(resp.data["params"]["detail"])

    def test_throttled_is_enveloped_and_keeps_retry_after(self):
        def _get(self, request):
            raise Throttled(wait=30)

        resp = _view(get=_get)(_factory.get("/"))
        assert resp.status_code == 429
        assert resp.data["localizable_error"] == ERR_429_TOO_MANY_REQUESTS
        assert resp["Retry-After"] == "30"
        assert resp.data["params"]["retry_after"] == 30

    def test_django_permission_denied_is_enveloped(self):
        from django.core.exceptions import PermissionDenied as DjangoPermissionDenied

        def _get(self, request):
            raise DjangoPermissionDenied("nope")

        resp = _view(get=_get)(_factory.get("/"))
        assert resp.status_code == 403
        assert resp.data["localizable_error"] == ERR_403_FORBIDDEN

    # --- key resolution ---

    def test_default_code_naming_a_registered_key_wins_over_the_status(self):
        """MandateUnavailable (503) has no generic status key and does not need
        one — its default_code names the registered key directly."""
        from stapel_core.django.api.permissions import MandateUnavailable

        resp = stapel_exception_handler(MandateUnavailable(), _ctx())
        assert resp.status_code == 503
        assert resp.data["localizable_error"] == "error.503.mandate_unavailable"

    def test_unmapped_status_keeps_drfs_shape(self):
        """A status with neither a registered key nor a mapping is left alone
        rather than given an invented code — StapelServiceError is the way to
        raise an enveloped error on an arbitrary status."""

        class _Teapot(APIException):
            status_code = 418
            default_detail = "I am a teapot."
            default_code = "teapot"

        resp = stapel_exception_handler(_Teapot(), _ctx())
        assert resp.status_code == 418
        assert resp.data == {"detail": "I am a teapot."}

    def test_validation_error_tier_is_unchanged(self):
        exc = DRFValidationError(
            {"email": [ErrorDetail("This field is required.", code="required")]}
        )
        resp = stapel_exception_handler(exc, _ctx())
        assert resp.status_code == 400
        assert resp.data["localizable_error"] == "error.400.field.required"
        assert resp.data["params"]["field"] == "email"

    def test_non_api_exception_still_falls_through_to_none(self):
        assert stapel_exception_handler(ValueError("nope"), _ctx()) is None

    def test_every_mapped_key_is_registered(self):
        from stapel_core.django.api.errors import (
            _DRF_STATUS_ERROR_KEYS,
            build_error_registry,
        )

        known = {e["code"] for e in build_error_registry()}
        assert set(_DRF_STATUS_ERROR_KEYS.values()) <= known
        # The status a key names must match the status it is served under.
        for status_code, key in _DRF_STATUS_ERROR_KEYS.items():
            assert key.split(".")[1] == str(status_code)
