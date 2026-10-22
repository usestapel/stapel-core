"""A provider's LookupError survives call() as a LookupError.

In-process ``call()`` wrapped every provider exception in a bare
``FunctionCallError``, so a caller written against the provider's documented
``LookupError`` ("unknown category") never caught it and answered 500.
``FunctionLookupError`` is both, on the in-process and the HTTP transport.
"""
import pytest
from django.test import override_settings
from rest_framework.test import APIRequestFactory

from stapel_core.comm import (
    FunctionCallError,
    FunctionLookupError,
    call,
    register_function,
)
from stapel_core.comm.registry import function_registry


@pytest.fixture
def lookup_provider():
    def provider(payload):
        if payload.get("id") == 1:
            return {"id": 1}
        if payload.get("id") == 0:
            raise RuntimeError("database down")
        raise LookupError(f"thing {payload.get('id')} not found")

    register_function("things.get", provider)
    yield
    function_registry._providers.pop("things.get", None)
    function_registry._schemas.pop("things.get", None)


def test_in_process_lookup_error_is_both(lookup_provider):
    with pytest.raises(LookupError) as exc:
        call("things.get", {"id": 9})
    assert isinstance(exc.value, FunctionLookupError)
    assert isinstance(exc.value, FunctionCallError)
    assert "thing 9 not found" in str(exc.value)


def test_other_failures_stay_plain_call_errors(lookup_provider):
    with pytest.raises(FunctionCallError) as exc:
        call("things.get", {"id": 0})
    assert not isinstance(exc.value, LookupError)


def test_remote_view_answers_lookup_as_422(lookup_provider):
    from stapel_core.comm.http import FunctionCallView

    request = APIRequestFactory().post(
        "/api/_functions/things.get/", {"payload": {"id": 9}}, format="json"
    )
    request.is_service_request = True
    view = FunctionCallView.as_view(permission_classes=[])
    resp = view(request, name="things.get")
    assert resp.status_code == 422
    assert resp.data == {"error": "thing 9 not found", "kind": "lookup"}


def test_http_transport_reraises_a_lookup_answer(monkeypatch):
    class FakeResponse:
        status_code = 422
        text = '{"error": "thing 9 not found", "kind": "lookup"}'
        headers = {"Content-Type": "application/json"}

        def json(self):
            return {"error": "thing 9 not found", "kind": "lookup"}

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return FakeResponse()

    from stapel_core.comm import functions as functions_mod

    monkeypatch.setattr(functions_mod, "_http_session", lambda: FakeSession())
    with override_settings(STAPEL_COMM={
        "FUNCTION_TRANSPORT": "http",
        "FUNCTION_ROUTES": {"things.": "http://svc-things:8000"},
    }):
        with pytest.raises(LookupError):
            call("things.get", {"id": 9})
