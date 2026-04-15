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
    servers: list[dict[str, Any]] | None = None,
    terms_of_service: str | None = None,
    contact: dict[str, Any] | None = None,
    license_info: dict[str, Any] | None = None,
    openapi_tags: list[dict[str, Any]] | None = None,
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
    if terms_of_service:
        schema["info"]["termsOfService"] = terms_of_service
    if contact:
        schema["info"]["contact"] = contact
    if license_info:
        schema["info"]["license"] = license_info

    if servers:
        schema["servers"] = servers

    if openapi_tags:
        schema["tags"] = openapi_tags

    components_schemas: dict[str, Any] = {}
    security_schemes: dict[str, Any] = {}

    for route in routes:
        path = route["path"]

        # Collect security schemes from route dependencies
        _collect_security_schemes(route, security_schemes)

        for method in route["methods"]:
            operation = _build_operation(route, method.lower())
            schema["paths"].setdefault(path, {})[method.lower()] = operation

            # Collect Pydantic model schemas into components
            _collect_schemas(route, components_schemas)

    components: dict[str, Any] = {}
    if components_schemas:
        components["schemas"] = components_schemas
    if security_schemes:
        components["securitySchemes"] = security_schemes
    if components:
        schema["components"] = components

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


def _collect_security_schemes(
    route: dict[str, Any], security_schemes: dict[str, Any]
) -> None:
    """Extract security schemes from route dependency parameters.

    Checks both ``params`` and ``_all_params`` (the latter preserves
    dependency params even after handler compilation strips them).
    """
    # Use _all_params if available (it includes dep params before compilation),
    # falling back to params.
    all_params = route.get("_all_params", route.get("params", []))
    for param in all_params:
        dep_callable = param.get("dep_callable")
        if dep_callable is None:
            # Also check the original dep callable
            dep_callable = param.get("_original_dep_callable")
        if dep_callable is None:
            continue

        # Security schemes are instances of classes with a .model dict
        # (OAuth2PasswordBearer, HTTPBearer, HTTPBasic, APIKeyHeader, etc.)
        obj = dep_callable
        if hasattr(obj, "model") and isinstance(obj.model, dict):
            scheme_name = getattr(obj, "scheme_name", None) or type(obj).__name__
            if scheme_name not in security_schemes:
                security_schemes[scheme_name] = obj.model


def _type_hint_to_schema(type_hint: str) -> dict[str, Any]:
    """Map a simple type-hint string to an OpenAPI schema fragment."""
    mapping: dict[str, dict[str, Any]] = {
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "str": {"type": "string"},
    }
    return dict(mapping.get(type_hint, {"type": "string"}))
