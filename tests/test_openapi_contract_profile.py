"""Contract profile 2 (stapel_core.django.openapi.contract).

Every rule is tested twice: the profile-2 behaviour, and its inversion — the
same view under profile 1 (or with the one fact flipped) does not get it.
"""
import pytest
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers
from rest_framework.views import APIView

from stapel_core.django.api import errors as core_errors
from stapel_core.django.api.errors import (
    StapelErrorResponse,
    StapelResponse,
    error_401_unauthorized,
)
from stapel_core.django.captcha import CaptchaMixin, captcha_protected
from stapel_core.django.jwt.authentication import JWTCookieAuthentication
from stapel_core.django.openapi.contract import (
    ContractProfileError,
    _CodeWalker,
    contract_profile,
    current_contract_profile,
    raises,
)
from stapel_core.django.openapi.extensions import _openapi_path
from stapel_core.django.openapi.schemas import PermissionAwareAutoSchema
from stapel_core.django.openapi.swagger import _register_jwt_auth_extension
from stapel_core.verification.decorators import requires_verification

ERR_409_TAKEN = "error.409.contract_probe_taken"
ERR_400_BAD = "error.400.contract_probe_bad"
ERR_418_NEVER = "error.418.contract_probe_never"

_register_jwt_auth_extension()


@pytest.fixture(autouse=True)
def _probe_codes(monkeypatch):
    for code in (ERR_409_TAKEN, ERR_400_BAD, ERR_418_NEVER):
        monkeypatch.setitem(core_errors._GLOBAL_REGISTRY, code, "probe")


class AdmitsStrangers(permissions.BasePermission):
    """Like stapel-auth's DenyEnrollOnly: refuses only a particular token."""

    def has_permission(self, request, view):
        return not request.headers.get("X-Enroll-Only")


class RaisesWhenProbed(permissions.BasePermission):
    def has_permission(self, request, view):
        raise RuntimeError("needs a database")


def _taken_helper(flag):
    if flag:
        return StapelErrorResponse(409, ERR_409_TAKEN)
    return None


def _never_called():
    return StapelErrorResponse(418, ERR_418_NEVER)


class BodySerializer(serializers.Serializer):
    name = serializers.CharField()

    def validate_name(self, value):
        if value == "bad":
            raise serializers.ValidationError(ERR_400_BAD)
        return value


class CaptchaBodySerializer(CaptchaMixin, serializers.Serializer):
    name = serializers.CharField()


class OutSerializer(serializers.Serializer):
    ok = serializers.BooleanField()


class _Base(APIView):
    schema = PermissionAwareAutoSchema()
    authentication_classes = [JWTCookieAuthentication]


class PublicView(_Base):
    permission_classes = [AdmitsStrangers]

    @extend_schema(request=BodySerializer, responses={200: OutSerializer})
    def post(self, request):
        """
Docstring at column 0 — dedent() cannot handle this method's source.
"""
        return _taken_helper(request.data.get("taken")) or StapelResponse({"ok": True})


class PrivateView(_Base):
    permission_classes = [permissions.IsAuthenticated, AdmitsStrangers]

    @extend_schema(request=BodySerializer, responses={200: OutSerializer})
    def post(self, request):
        return _taken_helper(True)


class ProbeRaisesView(_Base):
    permission_classes = [RaisesWhenProbed]

    @extend_schema(responses={200: OutSerializer})
    def get(self, request):
        return StapelResponse({"ok": True})


class BodyGateView(_Base):
    permission_classes = [AdmitsStrangers]

    @extend_schema(responses={200: OutSerializer})
    def get(self, request):
        if not request.user.is_authenticated:
            return error_401_unauthorized()
        return StapelResponse({"ok": True})


class DeclaredView(_Base):
    permission_classes = [permissions.AllowAny]

    @extend_schema(responses={200: OutSerializer})
    @raises(ERR_409_TAKEN)
    def get(self, request):
        return StapelResponse({"ok": True})


class CaptchaDecoratedView(_Base):
    permission_classes = [permissions.AllowAny]

    @extend_schema(request=None, responses={200: OutSerializer})
    @captcha_protected(action="register")
    def post(self, request):
        return StapelResponse({"ok": True})


class CaptchaMixinView(_Base):
    permission_classes = [permissions.AllowAny]

    @extend_schema(request=CaptchaBodySerializer, responses={200: OutSerializer})
    def post(self, request):
        return StapelResponse({"ok": True})


class VerifiedView(_Base):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: OutSerializer})
    @requires_verification(scope="probe.payout", factors=["totp"], max_age=300)
    def delete(self, request):
        return StapelResponse({"ok": True})


def _schema(view, profile, path="/probe/"):
    from django.urls import path as dj_path

    patterns = [dj_path(path.strip("/") + "/", view.as_view())]
    generator = SchemaGenerator(patterns=patterns)
    with contract_profile(profile):
        assert current_contract_profile() == profile
        return generator.get_schema(request=None, public=True)


def _op(schema):
    (item,) = schema["paths"].values()
    (op,) = [o for m, o in item.items() if isinstance(o, dict)]
    return op


# -- profile switch ---------------------------------------------------------


def test_profile_defaults_to_1_and_rejects_unknown():
    assert current_contract_profile() == 1
    with pytest.raises(ValueError):
        with contract_profile(3):
            pass


# -- (1) bearer -------------------------------------------------------------


def test_profile2_declares_bearer_next_to_cookie():
    schema = _schema(PrivateView, 2)
    schemes = schema["components"]["securitySchemes"]
    assert schemes["JWTBearerAuth"]["scheme"] == "bearer"
    assert schemes["JWTCookieAuth"]["in"] == "cookie"
    assert _op(schema)["security"] == [{"JWTCookieAuth": []}, {"JWTBearerAuth": []}]


def test_profile1_has_no_bearer():
    schema = _schema(PrivateView, 1)
    assert set(schema["components"]["securitySchemes"]) == {"JWTCookieAuth"}
    assert _op(schema)["security"] == [{"JWTCookieAuth": []}]


# -- (2) public operations from the running gate ------------------------------


def test_gate_admitting_strangers_is_public_in_profile2():
    assert {} in _op(_schema(PublicView, 2))["security"]


def test_inversion_same_gate_is_auth_required_in_profile1():
    assert {} not in _op(_schema(PublicView, 1))["security"]


def test_inversion_gate_with_isauthenticated_is_not_public():
    assert {} not in _op(_schema(PrivateView, 2))["security"]


def test_gate_that_raises_when_probed_is_not_public():
    assert {} not in _op(_schema(ProbeRaisesView, 2))["security"]


def test_handler_that_answers_401_itself_is_not_public():
    op = _op(_schema(BodyGateView, 2))
    assert {} not in op["security"]
    assert op["responses"]["401"]["x-stapel-errors"] == ["error.401.unauthorized"]


# -- (5) error codes ----------------------------------------------------------


def test_codes_reached_from_handler_and_serializer_are_bound():
    op = _op(_schema(PublicView, 2))
    assert op["responses"]["409"]["x-stapel-errors"] == [ERR_409_TAKEN]
    assert ERR_400_BAD in op["responses"]["400"]["x-stapel-errors"]
    # a status the declaration did not list is added with the error envelope
    assert op["responses"]["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/StapelError"
    }


def test_inversion_code_in_an_unreached_function_is_not_bound():
    op = _op(_schema(PublicView, 2))
    bound = {c for r in op["responses"].values() for c in r.get("x-stapel-errors", [])}
    assert ERR_418_NEVER not in bound


def test_inversion_profile1_binds_nothing():
    op = _op(_schema(PublicView, 1))
    assert not any("x-stapel-errors" in r for r in op["responses"].values())
    assert "409" not in op["responses"]


def test_raises_declaration_is_bound():
    op = _op(_schema(DeclaredView, 2))
    assert op["responses"]["409"]["x-stapel-errors"] == [ERR_409_TAKEN]


def test_raises_rejects_a_malformed_code():
    with pytest.raises(ValueError):
        raises("not-a-code")


def test_raises_an_unregistered_code_refuses_emission():
    class Undeclared(_Base):
        permission_classes = [permissions.AllowAny]

        @extend_schema(responses={200: OutSerializer})
        @raises("error.409.contract_probe_unregistered")
        def get(self, request):
            return StapelResponse({"ok": True})

    with pytest.raises(ContractProfileError):
        _schema(Undeclared, 2)


def test_walker_reads_unregistered_strings_as_nothing():
    walker = _CodeWalker({ERR_409_TAKEN: 409}, packages=(__name__.split(".")[0],))

    def handler():
        return "error.400.not_registered", ERR_409_TAKEN

    walker.walk(handler)
    assert walker.codes == {ERR_409_TAKEN}


# -- captcha ------------------------------------------------------------------


def test_captcha_decorator_is_emitted_with_its_action():
    op = _op(_schema(CaptchaDecoratedView, 2))
    assert op["x-stapel-captcha"] == {
        "action": "register",
        "token": {"header": "X-Captcha-Token", "field": "captcha_token"},
    }
    codes = op["responses"]["400"]["x-stapel-errors"]
    assert "error.400.captcha_required" in codes


def test_captcha_mixin_serializer_is_emitted_without_action():
    op = _op(_schema(CaptchaMixinView, 2))
    assert op["x-stapel-captcha"] == {"action": None, "token": {"field": "captcha_token"}}


def test_inversion_no_captcha_no_key():
    assert "x-stapel-captcha" not in _op(_schema(DeclaredView, 2))
    assert "x-stapel-captcha" not in _op(_schema(CaptchaDecoratedView, 1))


# -- verification -------------------------------------------------------------


def test_verification_403_keeps_the_challenge_schema():
    op = _op(_schema(VerifiedView, 2))
    challenge = op["responses"]["403"]
    assert "verification" in challenge["content"]["application/json"]["schema"]["properties"]
    assert "error.403.verification_required" in challenge["x-stapel-errors"]


# -- flows hook path matching ---------------------------------------------------


def test_openapi_path_handles_router_regex_patterns():
    assert _openapi_path("api/^keys/(?P<pk>[^/.]+)$") == "/api/keys/{pk}"
    assert _openapi_path("api/items/<int:pk>/") == "/api/items/{pk}/"


# -- /site/ declares its body ---------------------------------------------------


def test_site_bootstrap_declares_a_response_schema():
    from stapel_core.django.sites.views import SiteBootstrapView

    class Site(SiteBootstrapView):
        schema = PermissionAwareAutoSchema()

    op = _op(_schema(Site, 1, path="/site/"))
    assert op["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SiteBootstrap"
    }


# -- (6) the flows hook reads the registry --------------------------------------


class FlowBoundView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        return StapelResponse({"ok": True})


def test_flows_hook_binds_registry_steps_and_router_paths():
    import sys
    import types

    from django.test import override_settings
    from django.urls import path, re_path

    from stapel_core.flows import Flow
    from stapel_core.flows.registry import flow_registry
    from stapel_core.django.openapi.extensions import stapel_postprocessing_hook

    flow = Flow("contract.probe_flow", title="probe", description="probe")
    ref = f"{FlowBoundView.post.__module__}.{FlowBoundView.post.__qualname__}"
    # A step bound by ref (not by an attribute on the handler).
    flow._http(ref, order=1, note="post", note_key="", extra={})
    module = types.ModuleType("_contract_flow_urls")
    module.urlpatterns = [
        path("bound/", FlowBoundView.as_view()),
        re_path(r"^keys/(?P<pk>[^/.]+)$", FlowBoundView.as_view()),
    ]
    sys.modules["_contract_flow_urls"] = module
    result = {"paths": {
        "/bound/": {"post": {"operationId": "bound_create"}},
        "/keys/{id}": {"post": {"operationId": "keys_create"}},
        "/elsewhere/": {"post": {"operationId": "elsewhere_create"}},
    }}
    try:
        with override_settings(ROOT_URLCONF="_contract_flow_urls"):
            out = stapel_postprocessing_hook(result, None, None, True)
    finally:
        del sys.modules["_contract_flow_urls"]
        flow_registry._flows.pop(flow.id, None)
    assert out["paths"]["/bound/"]["post"]["x-stapel-flows"] == ["contract.probe_flow"]
    # regex route, schema renamed pk -> id: matched by shape
    assert out["paths"]["/keys/{id}"]["post"]["x-stapel-flows"] == ["contract.probe_flow"]
    assert "x-stapel-flows" not in out["paths"]["/elsewhere/"]["post"]


# -- credentials checked in the handler body ----------------------------------

from stapel_core.django.openapi.contract import Credential, accepts_credentials  # noqa: E402

PROBE_TOKEN = Credential(
    "ProbeTokenAuth", {"type": "apiKey", "in": "header", "name": "X-Probe-Token"}
)


class TokenOrSessionView(_Base):
    """Open at the DRF layer; the body accepts a session or the probe token."""

    permission_classes = [permissions.AllowAny]

    @extend_schema(responses={200: OutSerializer})
    @accepts_credentials(PROBE_TOKEN)
    def get(self, request):
        return StapelResponse({"ok": True})


class TokenOnlyView(_Base):
    permission_classes = [permissions.AllowAny]

    @extend_schema(responses={200: OutSerializer})
    @accepts_credentials(PROBE_TOKEN, session=False)
    def get(self, request):
        return StapelResponse({"ok": True})


@accepts_credentials(PROBE_TOKEN, session=False, anonymous=True)
class TokenOrBodyView(_Base):
    permission_classes = [permissions.AllowAny]

    @extend_schema(responses={200: OutSerializer})
    def get(self, request):
        return StapelResponse({"ok": True})


@pytest.mark.parametrize("profile", [1, 2])
def test_declared_credential_is_a_scheme_and_an_alternative(profile):
    schema = _schema(TokenOrSessionView, profile)
    assert schema["components"]["securitySchemes"]["ProbeTokenAuth"] == PROBE_TOKEN.scheme
    security = _op(schema)["security"]
    assert {"ProbeTokenAuth": []} in security
    assert {"JWTCookieAuth": []} in security
    assert {} not in security


def test_inversion_undeclared_open_view_is_public():
    # The same view without the declaration: profile 2 calls it open.
    class Undeclared(_Base):
        permission_classes = [permissions.AllowAny]

        @extend_schema(responses={200: OutSerializer})
        def get(self, request):
            return StapelResponse({"ok": True})

    schema = _schema(Undeclared, 2)
    assert {} in _op(schema)["security"]
    assert "ProbeTokenAuth" not in schema["components"].get("securitySchemes", {})


@pytest.mark.parametrize("profile", [1, 2])
def test_credential_without_session_is_the_only_requirement(profile):
    assert _op(_schema(TokenOnlyView, profile))["security"] == [{"ProbeTokenAuth": []}]


def test_class_level_declaration_keeps_the_anonymous_alternative():
    assert _op(_schema(TokenOrBodyView, 2))["security"] == [{"ProbeTokenAuth": []}, {}]


def test_accepts_credentials_rejects_a_bare_scheme():
    with pytest.raises(TypeError):
        accepts_credentials({"type": "apiKey"})
