from dataclasses import dataclass

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ErrorDetail
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIRequestFactory
from stapel_core.django.api.errors import (
    ERR_400_BAD_REQUEST,
    ERR_403_FORBIDDEN,
    ERR_404_NOT_FOUND,
    ERR_429_RATE_LIMIT,
    ERR_500_INTERNAL,
    StapelErrorResponse,
    StapelResponse,
    StapelServiceError,
    StapelValidationError,
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
