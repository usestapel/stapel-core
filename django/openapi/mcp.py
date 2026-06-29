"""Helpers for generating MCP schemas from OpenAPI."""

from typing import Any, Callable, Dict

from django.http import JsonResponse


def convert_openapi_to_openrpc(schema: Dict[str, Any], request) -> Dict[str, Any]:
    """Convert OpenAPI schema dict to OpenRPC-compatible MCP schema."""
    mcp_schema = {
        "openrpc": "1.0.0",
        "info": {
            "title": schema.get("info", {}).get("title", "API"),
            "version": schema.get("info", {}).get("version", "1.0.0"),
            "description": schema.get("info", {}).get("description", ""),
        },
        "servers": [
            {
                "url": request.build_absolute_uri("/"),
                "name": "Current Server",
            }
        ],
        "methods": [],
        "components": {"schemas": schema.get("components", {}).get("schemas", {})},
    }

    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in ["get", "post", "put", "delete", "patch"]:
                continue

            mcp_method = {
                "name": operation.get(
                    "operationId", f"{method}_{path}".replace("/", "_")
                ),
                "summary": operation.get("summary", ""),
                "description": operation.get("description", ""),
                "params": {"type": "object", "properties": {}, "required": []},
                "result": {},
            }

            for param in operation.get("parameters", []):
                param_name = param.get("name")
                if not param_name:
                    continue
                param_schema = param.get("schema", {})
                mcp_method["params"]["properties"][param_name] = param_schema
                if param.get("required"):
                    mcp_method["params"]["required"].append(param_name)

            request_body = operation.get("requestBody", {})
            if request_body:
                content = request_body.get("content", {})
                json_content = content.get("application/json", {})
                if json_content:
                    mcp_method["params"] = json_content.get(
                        "schema", mcp_method["params"]
                    )

            responses = operation.get("responses", {})
            success_response = (
                responses.get("200") or responses.get("201") or responses.get("204")
            )
            if success_response:
                response_content = success_response.get("content", {})
                json_response = response_content.get("application/json", {})
                if json_response:
                    mcp_method["result"] = json_response.get("schema", {})

            mcp_schema["methods"].append(mcp_method)

    return mcp_schema


def convert_openapi_to_tools_schema(
    schema: Dict[str, Any],
    _request,
    *,
    name: str,
    description: str,
    version: str = "1.0.0",
) -> Dict[str, Any]:
    """
    Convert OpenAPI schema into Claude-compatible tools list format.

    This matches the older stapel-cdn MCP response shape to avoid breaking consumers.
    """
    mcp_schema = {
        "name": name,
        "version": version,
        "description": description,
        "tools": [],
    }

    for path, methods in schema.get("paths", {}).items():
        for method, details in methods.items():
            if method.lower() not in ["get", "post", "put", "patch", "delete"]:
                continue

            tool = {
                "name": details.get(
                    "operationId", f"{method}_{path}".replace("/", "_")
                ),
                "description": details.get("summary", details.get("description", "")),
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            }

            for param in details.get("parameters", []):
                param_name = param.get("name")
                if not param_name:
                    continue
                param_schema = param.get("schema", {})
                tool["inputSchema"]["properties"][param_name] = {
                    "type": param_schema.get("type", "string"),
                    "description": param.get("description", ""),
                }
                if param.get("required"):
                    tool["inputSchema"]["required"].append(param_name)

            request_body = details.get("requestBody", {})
            if request_body:
                content = request_body.get("content", {})
                json_body = content.get("application/json", {})
                if json_body:
                    tool["inputSchema"] = json_body.get("schema", tool["inputSchema"])

            mcp_schema["tools"].append(tool)

    return mcp_schema


def build_mcp_schema_view(
    title: str,
    description: str,
    version: str = "1.0.0",
    converter: Callable[
        [Dict[str, Any], Any], Dict[str, Any]
    ] = convert_openapi_to_openrpc,
):
    """
    Return a Django view that returns MCP schema converted from OpenAPI.

    Uses drf-spectacular to generate the OpenAPI schema.
    The converter controls the output format (OpenRPC, tools, etc.).

    Args:
        title: API title
        description: API description
        version: API version
        converter: Function to convert OpenAPI to MCP format
    """
    from drf_spectacular.generators import SchemaGenerator

    def mcp_schema_view(request):
        generator = SchemaGenerator(
            title=title,
            description=description,
            version=version,
        )
        schema = generator.get_schema(request=None, public=True)
        mcp_schema = converter(schema, request)
        return JsonResponse(mcp_schema, json_dumps_params={"indent": 2})

    return mcp_schema_view
