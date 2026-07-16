"""Coverage tests for stapel_core.django.openapi (schemas, swagger, mcp, openid)."""
import json
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
from django.http import HttpResponse
from drf_spectacular.openapi import AutoSchema
from rest_framework import serializers as drf_serializers

from stapel_core.django.openapi.mcp import (
    build_mcp_schema_view,
    convert_openapi_to_openrpc,
    convert_openapi_to_tools_schema,
)
from stapel_core.django.openapi.openid import (
    _b64url,
    _build_jwks,
    _build_openid_config,
    generate_jwks_to_dir,
)
from stapel_core.django.openapi.schemas import (
    COMMON_RESPONSES,
    MessageResponseSerializer,
    PermissionAwareAutoSchema,
    extend_schema_with_errors,
    get_error_responses,
    postprocess_fix_polymorphic_discriminators,
    postprocess_schema_tags,
    preprocess_exclude_schema_endpoints,
)
from stapel_core.django.openapi.swagger import (
    CustomSpectacularSwaggerView,
    IsStaffUserForSwagger,
    _register_jwt_auth_extension,
    get_app_swagger_urls,
    get_dev_urls,
    get_spectacular_settings,
    get_swagger_urls,
)


# ---------------------------------------------------------------------------
# schemas.py
# ---------------------------------------------------------------------------


class TestPermissionAwareAutoSchema:
    def _schema(self, permission_classes=None):
        schema = PermissionAwareAutoSchema()
        schema.view = SimpleNamespace(permission_classes=permission_classes or [])
        return schema

    def test_permission_info_empty(self):
        assert self._schema()._get_permission_info() == ""

    def test_permission_info_classes_and_instances(self):
        from stapel_core.django.api.permissions import IsStaffUser

        info = self._schema([IsStaffUser, IsStaffUser()])._get_permission_info()
        assert "**Permissions:**" in info
        assert "IsStaffUser, IsStaffUser" in info

    def test_field_meta_example_from_annotation(self):
        schema = self._schema()
        fld = drf_serializers.CharField()
        fld._spectacular_annotation = {"example": 7}
        with mock.patch.object(
            AutoSchema, "_get_serializer_field_meta", return_value={}
        ):
            meta = schema._get_serializer_field_meta(fld, "response")
        assert meta == {"example": 7}

    def test_field_meta_without_example(self):
        schema = self._schema()
        fld = drf_serializers.CharField()
        with mock.patch.object(
            AutoSchema, "_get_serializer_field_meta", return_value={}
        ):
            meta = schema._get_serializer_field_meta(fld, "response")
        assert "example" not in meta

    def _get_operation(self, schema, base_return):
        with mock.patch.object(AutoSchema, "get_operation", return_value=base_return):
            return schema.get_operation("/x/", "", "", "GET", None)

    def test_get_operation_none_passthrough(self):
        from stapel_core.django.api.permissions import IsStaffUser

        assert self._get_operation(self._schema([IsStaffUser]), None) is None

    def test_get_operation_appends_to_description(self):
        from stapel_core.django.api.permissions import IsStaffUser

        op = self._get_operation(
            self._schema([IsStaffUser]), {"description": "Base desc"}
        )
        assert op["description"].startswith("Base desc")
        assert "**Permissions:** `IsStaffUser`" in op["description"]

    def test_get_operation_sets_description_when_missing(self):
        from stapel_core.django.api.permissions import IsStaffUser

        op = self._get_operation(self._schema([IsStaffUser]), {"summary": "s"})
        assert op["description"] == "**Permissions:** `IsStaffUser`"

    def test_get_operation_no_permissions(self):
        op = self._get_operation(self._schema(), {"description": "Base"})
        assert op["description"] == "Base"


class TestResponseHelpers:
    def test_get_error_responses_filters_unknown(self):
        responses = get_error_responses(400, 404, 999)
        assert set(responses) == {400, 404}
        assert responses[400] is COMMON_RESPONSES[400]

    def test_extend_schema_with_errors(self):
        @extend_schema_with_errors(
            400, 404, description="d", responses={200: MessageResponseSerializer}
        )
        def myview(self, request):
            return None

        assert callable(myview)


class TestSpectacularHooks:
    def test_preprocess_excludes_well_known(self):
        endpoints = [
            ("/api/items/", "re", "GET", None),
            ("/.well-known/jwks.json", "re", "GET", None),
        ]
        result = preprocess_exclude_schema_endpoints(endpoints)
        assert [e[0] for e in result] == ["/api/items/"]

    def test_postprocess_schema_tags(self):
        result = {
            "paths": {
                "/api/auth/login/": {
                    "get": {"tags": ["auth"]},
                    "parameters": [],
                },
                "/api/foo/": {"post": {"tags": ["Custom Thing"]}},
            }
        }
        out = postprocess_schema_tags(result, None, None, True)
        tags = {t["name"]: t.get("description") for t in out["tags"]}
        assert tags["auth"] == "Authentication and authorization endpoints"
        assert tags["Custom Thing"] is None

    def test_postprocess_schema_tags_no_tags(self):
        out = postprocess_schema_tags({"paths": {}}, None, None, True)
        assert "tags" not in out

    def test_postprocess_fix_discriminators(self):
        result = {
            "components": {
                "schemas": {
                    "NoOneOf": {"type": "object"},
                    "NoDisc": {"oneOf": [{"$ref": "#/components/schemas/A"}]},
                    "DiscNoMapping": {
                        "oneOf": [{"$ref": "#/components/schemas/A"}],
                        "discriminator": {"propertyName": "status"},
                    },
                    "Union": {
                        "oneOf": [
                            {"$ref": "#/components/schemas/A"},
                            {"$ref": "#/components/schemas/B"},
                            {"type": "object"},
                        ],
                        "discriminator": {
                            "propertyName": "status",
                            "mapping": {"A": "#/components/schemas/A"},
                        },
                    },
                    "A": {
                        "properties": {
                            "status": {"$ref": "#/components/schemas/StatusEnum"}
                        }
                    },
                    "B": {"properties": {"status": {"enum": ["B1"]}}},
                    "StatusEnum": {"enum": ["A1", "A2"]},
                    "NonEnumUnion": {
                        "oneOf": [{"$ref": "#/components/schemas/C"}],
                        "discriminator": {
                            "propertyName": "kind",
                            "mapping": {"C": "#/components/schemas/C"},
                        },
                    },
                    "C": {"properties": {"kind": {"type": "string"}}},
                }
            }
        }
        out = postprocess_fix_polymorphic_discriminators(result, None, None, True)
        schemas = out["components"]["schemas"]
        assert schemas["Union"]["discriminator"]["mapping"] == {
            "A1": "#/components/schemas/A",
            "A2": "#/components/schemas/A",
            "B1": "#/components/schemas/B",
        }
        # Non-enum discriminator untouched
        assert schemas["NonEnumUnion"]["discriminator"]["mapping"] == {
            "C": "#/components/schemas/C"
        }

    def test_postprocess_fix_discriminators_empty_result(self):
        assert postprocess_fix_polymorphic_discriminators({}, None, None, True) == {}


# ---------------------------------------------------------------------------
# swagger.py
# ---------------------------------------------------------------------------


class TestGetSpectacularSettings:
    def test_merges_title_and_extras(self):
        s = get_spectacular_settings("My API", "Desc", version="2.0", CUSTOM="x")
        assert s["TITLE"] == "My API"
        assert s["DESCRIPTION"] == "Desc"
        assert s["VERSION"] == "2.0"
        assert s["CUSTOM"] == "x"

    def test_base_settings_not_mutated(self):
        from stapel_core.django.settings import SPECTACULAR_SETTINGS

        get_spectacular_settings("Other", "D")
        assert SPECTACULAR_SETTINGS["TITLE"] == "Stapel API"

    def test_version_defaults_without_package(self):
        # Historical default preserved when neither version nor package given.
        assert get_spectacular_settings("T", "D")["VERSION"] == "1.0.0"

    def test_version_resolved_from_package_metadata(self):
        from importlib.metadata import version as dist_version

        # info.version = the real installed package version (api-versioning
        # plan step 0: no more placeholder lies in Swagger UI / schema.json).
        s = get_spectacular_settings("T", "D", package="stapel-core")
        assert s["VERSION"] == dist_version("stapel-core")

    def test_explicit_version_wins_over_package(self):
        s = get_spectacular_settings("T", "D", version="9.9.9", package="stapel-core")
        assert s["VERSION"] == "9.9.9"

    def test_unknown_package_falls_back(self):
        s = get_spectacular_settings("T", "D", package="stapel-does-not-exist")
        assert s["VERSION"] == "1.0.0"


class TestSwaggerUrls:
    def test_get_swagger_urls_names(self):
        urls = get_swagger_urls("auth/")
        assert [u.name for u in urls] == ["schema", "swagger-ui", "redoc"]
        assert str(urls[0].pattern) == "auth/schema/"

    def test_get_app_swagger_urls_adds_slash(self):
        assert len(get_app_swagger_urls("auth")) == 3
        assert len(get_app_swagger_urls("auth/")) == 3


class _RenderableResponse(HttpResponse):
    rendered = False

    def render(self):
        self.rendered = True
        return self


class TestCustomSwaggerView:
    def test_custom_script_contains_services(self):
        script = CustomSpectacularSwaggerView._get_custom_script(
            "auth",
            [
                {
                    "name": "Auth",
                    "prefix": "auth",
                    "admin_url": "/auth/admin/",
                    "swagger_url": "/auth/swagger/",
                }
            ],
        )
        assert b"stapel-topbar" in script
        assert b"/auth/admin/" in script

    def test_dispatch_injects_script(self, rf):
        from drf_spectacular.views import SpectacularSwaggerView

        view = CustomSpectacularSwaggerView.as_view(url_name="schema")
        base_resp = _RenderableResponse(b"<html>swagger-ui here</body></html>")
        with mock.patch.object(
            SpectacularSwaggerView, "dispatch", return_value=base_resp
        ):
            resp = view(rf.get("/swagger/"))
        assert base_resp.rendered is True
        assert b"stapel-topbar" in resp.content
        assert resp.content.endswith(b"</body></html>")

    def test_dispatch_skips_non_swagger_content(self, rf):
        from drf_spectacular.views import SpectacularSwaggerView

        view = CustomSpectacularSwaggerView.as_view(url_name="schema")
        base_resp = HttpResponse(b"<html>plain page</body></html>")
        with mock.patch.object(
            SpectacularSwaggerView, "dispatch", return_value=base_resp
        ):
            resp = view(rf.get("/swagger/"))
        assert b"stapel-topbar" not in resp.content


class TestIsStaffUserForSwagger:
    def _request(self, user):
        return SimpleNamespace(user=user)

    def test_staff_allowed(self):
        perm = IsStaffUserForSwagger()
        user = SimpleNamespace(is_authenticated=True, is_staff=True)
        assert perm.has_permission(self._request(user), None) is True

    def test_non_staff_denied(self):
        perm = IsStaffUserForSwagger()
        user = SimpleNamespace(is_authenticated=True, is_staff=False)
        assert perm.has_permission(self._request(user), None) is False

    def test_no_user_denied(self):
        perm = IsStaffUserForSwagger()
        assert perm.has_permission(self._request(None), None) is False


class TestJwtAuthExtension:
    def test_registration_returns_class(self):
        ext_cls = _register_jwt_auth_extension()
        assert ext_cls.name == "JWTCookieAuth"
        definition = ext_cls.get_security_definition(mock.Mock(), None)
        assert definition["type"] == "apiKey"
        assert definition["name"] == "stapel_jwt"


class TestGetDevUrls:
    def test_empty_in_production(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "production")
        assert get_dev_urls("auth/") == []

    def test_empty_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("DJANGO_ENV", raising=False)
        assert get_dev_urls("auth/") == []

    def test_local_with_mcp_view(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "local")

        def mcp_view(request):
            return None

        urls = get_dev_urls("auth/", mcp_view)
        names = [u.name for u in urls]
        assert names == ["schema", "swagger-ui", "redoc", "mcp-schema", "mcp-wellknown"]

    def test_dev_without_mcp_view(self, monkeypatch):
        monkeypatch.setenv("DJANGO_ENV", "dev")
        assert len(get_dev_urls("auth/")) == 3

    def test_local_with_debug_toolbar(self, monkeypatch, settings, tmp_path):
        import types

        from django.conf import settings as dj_settings

        monkeypatch.setenv("DJANGO_ENV", "local")
        fake = types.ModuleType("debug_toolbar")
        fake.__path__ = [str(tmp_path)]
        fake.urls = ([], "djdt")
        monkeypatch.setitem(sys.modules, "debug_toolbar", fake)
        settings.INSTALLED_APPS = list(dj_settings.INSTALLED_APPS) + ["debug_toolbar"]

        urls = get_dev_urls("auth/")
        assert str(urls[-1].pattern) == "auth/__debug__/"


# ---------------------------------------------------------------------------
# mcp.py
# ---------------------------------------------------------------------------


_OPENAPI = {
    "info": {"title": "T", "version": "2.0", "description": "D"},
    "components": {"schemas": {"Thing": {"type": "object"}}},
    "paths": {
        "/api/items/": {
            "get": {
                "operationId": "listItems",
                "summary": "List",
                "description": "ListDesc",
                "parameters": [
                    {"name": "q", "schema": {"type": "string"}, "required": True},
                    {"schema": {"type": "integer"}},  # nameless — skipped
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"type": "array"}}
                        }
                    }
                },
            },
            "post": {
                "summary": "Create",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    }
                },
                "responses": {
                    "201": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/x"}}
                        }
                    }
                },
            },
            "parameters": [],  # path-level key — not an HTTP method
        },
        "/api/other/": {
            "delete": {"responses": {"404": {"description": "nope"}}},
            "head": {"summary": "skipped"},
        },
    },
}


def _mcp_request():
    return SimpleNamespace(build_absolute_uri=lambda p: "http://testserver" + p)


class TestConvertOpenapiToOpenrpc:
    def test_structure(self):
        out = convert_openapi_to_openrpc(_OPENAPI, _mcp_request())
        assert out["openrpc"] == "1.0.0"
        assert out["info"]["title"] == "T"
        assert out["servers"][0]["url"] == "http://testserver/"
        assert out["components"]["schemas"] == {"Thing": {"type": "object"}}

    def test_methods(self):
        out = convert_openapi_to_openrpc(_OPENAPI, _mcp_request())
        by_name = {m["name"]: m for m in out["methods"]}
        assert set(by_name) == {"listItems", "post__api_items_", "delete__api_other_"}

        get_m = by_name["listItems"]
        assert get_m["params"]["properties"]["q"] == {"type": "string"}
        assert get_m["params"]["required"] == ["q"]
        assert get_m["result"] == {"type": "array"}

        post_m = by_name["post__api_items_"]
        assert post_m["params"]["type"] == "object"  # replaced by requestBody schema
        assert post_m["result"] == {"$ref": "#/x"}  # from the 201 response

        assert by_name["delete__api_other_"]["result"] == {}


class TestConvertOpenapiToToolsSchema:
    def test_structure(self):
        out = convert_openapi_to_tools_schema(
            _OPENAPI, None, name="svc", description="d", version="3.0"
        )
        assert out["name"] == "svc"
        assert out["version"] == "3.0"
        by_name = {t["name"]: t for t in out["tools"]}
        assert set(by_name) == {"listItems", "post__api_items_", "delete__api_other_"}

        get_t = by_name["listItems"]
        assert get_t["inputSchema"]["properties"]["q"]["type"] == "string"
        assert get_t["inputSchema"]["required"] == ["q"]
        assert get_t["description"] == "List"

        # requestBody replaces the inputSchema entirely
        post_t = by_name["post__api_items_"]
        assert post_t["inputSchema"]["properties"] == {"name": {"type": "string"}}


class TestBuildMcpSchemaView:
    def test_default_converter(self, rf):
        with mock.patch("drf_spectacular.generators.SchemaGenerator") as generator_cls:
            generator_cls.return_value.get_schema.return_value = {
                "info": {"title": "X"},
                "paths": {},
            }
            view = build_mcp_schema_view("X", "D")
            resp = view(rf.get("/mcp-schema.json"))
        data = json.loads(resp.content)
        assert data["openrpc"] == "1.0.0"
        assert data["info"]["title"] == "X"

    def test_custom_converter(self, rf):
        with mock.patch("drf_spectacular.generators.SchemaGenerator") as generator_cls:
            generator_cls.return_value.get_schema.return_value = {}
            view = build_mcp_schema_view(
                "X", "D", converter=lambda schema, request: {"ok": True}
            )
            resp = view(rf.get("/mcp-schema.json"))
        assert json.loads(resp.content) == {"ok": True}


# ---------------------------------------------------------------------------
# openid.py
# ---------------------------------------------------------------------------


def _public_pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


class TestGenerateJwks:
    def test_skips_for_hs256(self, settings, tmp_path, capsys):
        settings.JWT_ALGORITHM = "HS256"
        generate_jwks_to_dir(str(tmp_path / "wk"))
        assert "Skipping JWKS generation" in capsys.readouterr().out
        assert not (tmp_path / "wk").exists()

    def test_skips_without_public_key(self, settings, tmp_path, capsys):
        settings.JWT_ALGORITHM = "RS256"
        settings.JWT_PUBLIC_KEY = ""
        generate_jwks_to_dir(str(tmp_path / "wk"))
        assert "Skipping" in capsys.readouterr().out

    def test_generates_files_for_rs256(self, settings, tmp_path):
        settings.JWT_ALGORITHM = "RS256"
        settings.JWT_ISSUER = "https://issuer.example"
        settings.JWT_PUBLIC_KEY = _public_pem()
        out_dir = tmp_path / "wk"
        generate_jwks_to_dir(str(out_dir))

        jwks = json.loads((out_dir / "jwks.json").read_text())
        key = jwks["keys"][0]
        assert key["kty"] == "RSA"
        assert key["alg"] == "RS256"
        assert len(key["kid"]) == 16
        assert "=" not in key["n"]

        config = json.loads((out_dir / "openid-configuration").read_text())
        assert config["issuer"] == "https://issuer.example"
        assert config["jwks_uri"] == "https://issuer.example/.well-known/jwks.json"

    def test_build_jwks_invalid_pem(self):
        with pytest.raises(RuntimeError, match="Failed to parse RSA public key"):
            _build_jwks("not a pem")

    def test_build_openid_config(self):
        config = _build_openid_config("https://x")
        assert config["token_endpoint"] == "https://x/auth/api/token/"
        assert config["response_types_supported"] == ["token"]

    def test_b64url_no_padding(self):
        assert "=" not in _b64url(b"\x00\x01\x02")
        assert _b64url(b"hi") == "aGk"
