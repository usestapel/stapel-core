"""``jwt/status/`` must be visible to the contract, and unchanged on the wire.

The route is mounted by stapel-auth at ``v1/jwt/status/`` and is polled by the
admin session-timeout widget on every open admin tab. It was a plain Django
``View``, so drf-spectacular emitted no path for it: the consumer's
``docs/schema.json`` had no ``/auth/api/v1/jwt/status/`` entry and the contract
gate was green over a route it could not see.

Moving the view to DRF is only safe if nothing on the wire moves with it, so
the behavioural pins come first and stay: the same JSON keys, the same status
codes for the three cases, and — the property that got this route restored
after audit AUTH-02 retired its sibling — no minting of anything. It decodes
and reports the caller's own cookies, which is why it may run with no
authentication class at all.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import RequestFactory

from stapel_core.django.jwt.views import JWTStatusView

PROVIDER = "stapel_core.django.jwt.views.jwt_provider"
URLS = "tests.jwt_status_urls"

factory = RequestFactory()


def _request(cookies=None, user=None):
    req = factory.get("/api/v1/jwt/status/")
    req.COOKIES = cookies or {}
    req.session = MagicMock()
    req.user = user if user is not None else SimpleNamespace(is_authenticated=False)
    return req


def _body(resp):
    return json.loads(resp.content)


class TestTheContractCanSeeTheRoute:
    def test_the_schema_lists_the_route(self):
        """Red before the move: drf-spectacular emits nothing for a plain View.

        Generated with the schema class every Stapel service configures
        (stapel_core.testing) — the question is what the consumer's
        `make contract` sees, not what a bare DRF default would.
        """
        from drf_spectacular.generators import SchemaGenerator

        schema = SchemaGenerator(urlconf=URLS).get_schema(request=None, public=True)
        assert "/api/v1/jwt/status/" in schema["paths"], sorted(schema["paths"])
        operation = schema["paths"]["/api/v1/jwt/status/"]["get"]
        assert "200" in operation["responses"]

    def test_the_view_is_a_drf_view(self):
        from rest_framework.views import APIView

        assert issubclass(JWTStatusView, APIView)


class TestTheWireDidNotMove:
    def test_no_tokens_reports_unauthenticated(self):
        resp = JWTStatusView.as_view()(_request())
        assert resp.status_code == 200
        assert _body(resp) == {"authenticated": False, "message": "No tokens found"}

    def test_valid_tokens_report_the_middleware_user(self):
        """The user this endpoint reports is the one the JWT middleware put on
        the request — the view authenticates nobody itself."""
        with patch(PROVIDER) as provider:
            provider.handler.decode_token.side_effect = [{"exp": 111}, {"exp": 222}]
            with patch.object(
                JWTStatusView, "_presented_profile", staticmethod(lambda user: {"id": "u1"})
            ):
                resp = JWTStatusView.as_view()(_request(
                    cookies={"stapel_jwt": "acc.tok", "stapel_refresh_jwt": "ref.tok"},
                    user=SimpleNamespace(is_authenticated=True),
                ))
        assert resp.status_code == 200
        assert _body(resp) == {
            "authenticated": True,
            "profile": {"id": "u1"},
            "tokens": {
                "access_token_valid": True,
                "refresh_token_valid": True,
                "access_token_exp": 111,
                "refresh_token_exp": 222,
            },
        }

    def test_an_expired_token_is_reported_not_refused(self):
        with patch(PROVIDER) as provider:
            provider.handler.decode_token.return_value = None
            resp = JWTStatusView.as_view()(_request(cookies={"stapel_jwt": "acc.tok"}))
        assert resp.status_code == 200
        body = _body(resp)
        assert body["authenticated"] is False
        assert body["profile"] is None
        assert body["tokens"]["access_token_valid"] is False
        assert body["tokens"]["access_token_exp"] is None

    def test_a_failure_is_a_500_with_the_same_body(self):
        with patch(PROVIDER) as provider:
            provider.handler.decode_token.side_effect = RuntimeError("boom")
            resp = JWTStatusView.as_view()(_request(cookies={"stapel_jwt": "acc.tok"}))
        assert resp.status_code == 500
        assert _body(resp) == {"status": "error", "message": "Status check failed"}

    def test_it_authenticates_nobody_and_therefore_mints_nobody(self):
        """``JWTCookieAuthentication`` creates the local user row for a token
        it has never seen. A status probe must not do that, so this view runs
        with no authentication class whatever the deployment's default is."""
        assert JWTStatusView.authentication_classes == []

    def test_the_gate_is_open_like_the_plain_view_it_replaces(self):
        from rest_framework.permissions import AllowAny

        assert JWTStatusView.permission_classes == [AllowAny]
