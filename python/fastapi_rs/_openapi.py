"""OpenAPI 3.1.0 schema generation from route metadata.

Generates the JSON-serialisable schema dict at startup so it can be
served by the Rust core at ``/openapi.json``.
"""

from __future__ import annotations

from typing import Any


def generate_openapi_schema(
    *,
    title: str,
    version: str,
    description: str,
    routes: list[dict[str, Any]],
    openapi_url: str = "/openapi.json",
) -> dict[str, Any]:
    """Generate an OpenAPI 3.1.0 schema dict from collected route metadata."""
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": version,
        },
        "paths": {},
    }

    if description:
        schema["info"]["description"] = description

    components_schemas: dict[str, Any] = {}

    for route in routes:
        path = route["path"]

        for method in route["methods"]:
            operation = _build_operation(route, method.lower())
            schema["paths"].setdefault(path, {})[method.lower()] = operation

            # Collect Pydantic model schemas into components
            _collect_schemas(route, components_schemas)

    if components_schemas:
        schema["components"] = {"schemas": components_schemas}

    return schema


def _build_operation(route: dict[str, Any], method: str) -> dict[str, Any]:
    """Build an OpenAPI operation object for a single route+method."""
    operation: dict[str, Any] = {
        "summary": route.get("summary") or route["handler_name"],
        "operationId": f"{route['handler_name']}_{method}",
        "responses": {
            str(route.get("status_code", 200)): {
                "description": "Successful Response",
                "content": {"application/json": {"schema": {}}},
            }
        },
    }

    if route.get("tags"):
        operation["tags"] = route["tags"]
    if route.get("description"):
        operation["description"] = route["description"]
    if route.get("deprecated"):
        operation["deprecated"] = True

    # Parameters (path, query, header, cookie) and request body
    parameters: list[dict[str, Any]] = []
    request_body: dict[str, Any] | None = None

    for param in route.get("params", []):
        kind = param.get("kind", "")
        if kind in ("path", "query", "header", "cookie"):
            parameters.append(_build_parameter(param))
        elif kind == "body":
            request_body = _build_request_body(param)
        # Skip "dependency" params — they are internal

    if parameters:
        operation["parameters"] = parameters
    if request_body:
        operation["requestBody"] = request_body

    return operation


def _build_parameter(param: dict[str, Any]) -> dict[str, Any]:
    """Build an OpenAPI parameter object (path/query/header/cookie)."""
    p: dict[str, Any] = {
        "name": param.get("alias") or param["name"],
        "in": param["kind"],
        "required": param["required"],
        "schema": _type_hint_to_schema(param.get("type_hint", "str")),
    }
    if param.get("default_value") is not None:
        p["schema"]["default"] = param["default_value"]
    return p


def _build_request_body(param: dict[str, Any]) -> dict[str, Any]:
    """Build an OpenAPI requestBody from a body parameter."""
    model_class = param.get("model_class")
    if model_class is not None and hasattr(model_class, "model_json_schema"):
        body_schema = model_class.model_json_schema()
    else:
        body_schema = {"type": "object"}

    return {
        "required": param.get("required", True),
        "content": {
            "application/json": {
                "schema": body_schema,
            }
        },
    }


def _collect_schemas(
    route: dict[str, Any], schemas: dict[str, Any]
) -> None:
    """Extract Pydantic model ``$defs`` into the shared components/schemas bucket."""
    for param in route.get("params", []):
        model_class = param.get("model_class")
        if model_class is None:
            continue
        if not hasattr(model_class, "model_json_schema"):
            continue
        json_schema = model_class.model_json_schema()
        # Pydantic v2 puts referenced sub-models under "$defs"
        if "$defs" in json_schema:
            for name, defn in json_schema["$defs"].items():
                schemas.setdefault(name, defn)


def _type_hint_to_schema(type_hint: str) -> dict[str, Any]:
    """Map a simple type-hint string to an OpenAPI schema fragment."""
    mapping: dict[str, dict[str, Any]] = {
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "str": {"type": "string"},
    }
    return dict(mapping.get(type_hint, {"type": "string"}))
