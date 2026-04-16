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
        # Honor include_in_schema=False — skip route entirely
        if not route.get("include_in_schema", True):
            continue

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
    status_code = route.get("status_code") or 200
    response_desc = route.get("response_description") or "Successful Response"

    # Success response skeleton — overridable by route.responses[status]
    success_response: dict[str, Any] = {
        "description": response_desc,
        "content": {"application/json": {"schema": {}}},
    }

    # Build the full responses dict, starting with user-supplied responses
    # merged with the auto-generated success entry
    responses_dict: dict[str, Any] = {}
    user_responses = route.get("responses") or {}
    for status_key, resp_info in user_responses.items():
        responses_dict[str(status_key)] = _build_response_entry(resp_info)

    # Always include success response (route.responses may override it)
    success_key = str(status_code)
    if success_key not in responses_dict:
        responses_dict[success_key] = success_response
    else:
        # Merge user-supplied entry at success code with auto-generated description
        responses_dict[success_key].setdefault("description", response_desc)

    operation: dict[str, Any] = {
        "summary": route.get("summary") or route["handler_name"],
        "operationId": route.get("operation_id") or f"{route['handler_name']}_{method}",
        "responses": responses_dict,
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
        # Honor param-level include_in_schema
        if not param.get("include_in_schema", True):
            continue
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

    # Per-route security: None = auto-derive from deps; [] = empty; non-empty = override
    route_security = route.get("security")
    if route_security is not None:
        operation["security"] = route_security
    else:
        auto_security = _derive_security_from_deps(route)
        if auto_security:
            operation["security"] = auto_security

    # Callbacks (OpenAPI callbacks as nested operation dicts)
    callbacks = route.get("callbacks") or []
    if callbacks:
        operation["callbacks"] = _build_callbacks(callbacks)

    # Merge in openapi_extra (user's custom OpenAPI fields)
    openapi_extra = route.get("openapi_extra") or {}
    if openapi_extra:
        operation.update(openapi_extra)

    return operation


def _build_response_entry(resp_info: dict[str, Any]) -> dict[str, Any]:
    """Convert a user-supplied entry from route.responses into an OpenAPI response object.

    Accepts forms like:
        {"description": "Not found"}
        {"description": "Error", "model": MyError}
        {"description": "X", "content": {"application/json": {"schema": {...}}}}
    """
    entry: dict[str, Any] = {}
    entry["description"] = resp_info.get("description", "Response")

    # If model is provided, build content automatically
    model = resp_info.get("model")
    if model is not None and hasattr(model, "model_json_schema"):
        try:
            schema = model.model_json_schema()
            entry["content"] = {"application/json": {"schema": schema}}
        except Exception:
            pass

    # Direct content/headers overrides
    if "content" in resp_info:
        entry["content"] = resp_info["content"]
    if "headers" in resp_info:
        entry["headers"] = resp_info["headers"]
    if "links" in resp_info:
        entry["links"] = resp_info["links"]

    return entry


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
    if param.get("title"):
        p["schema"]["title"] = param["title"]
    if param.get("description"):
        p["description"] = param["description"]
    if param.get("deprecated"):
        p["deprecated"] = True
    # OpenAPI 3.1 example/examples
    if param.get("example") is not None:
        p["example"] = param["example"]
    if param.get("examples") is not None:
        p["examples"] = _normalize_examples(param["examples"])
    return p


def _build_request_body(param: dict[str, Any]) -> dict[str, Any]:
    """Build an OpenAPI requestBody from a body parameter."""
    model_class = param.get("model_class")
    if model_class is not None and hasattr(model_class, "model_json_schema"):
        body_schema = model_class.model_json_schema()
    else:
        body_schema = {"type": "object"}

    # media_type override (e.g., application/xml, application/octet-stream)
    media_type = param.get("media_type") or "application/json"

    content: dict[str, Any] = {media_type: {"schema": body_schema}}
    if param.get("example") is not None:
        content[media_type]["example"] = param["example"]
    if param.get("examples") is not None:
        content[media_type]["examples"] = _normalize_examples(param["examples"])

    body: dict[str, Any] = {
        "required": param.get("required", True),
        "content": content,
    }
    if param.get("description"):
        body["description"] = param["description"]
    return body


def _normalize_examples(examples: Any) -> dict[str, Any]:
    """Normalize examples into OpenAPI 3.1 named-examples form.

    Accepts:
        {"name1": {"value": ..., "summary": ...}, ...} — already normalized
        [{"name": "a", "value": ...}, ...] — list form, convert to dict
        ["foo", "bar"] — bare values, auto-name
    """
    if isinstance(examples, dict):
        return examples
    if isinstance(examples, list):
        result: dict[str, Any] = {}
        for i, ex in enumerate(examples):
            if isinstance(ex, dict) and "value" in ex:
                name = ex.pop("name", f"example{i + 1}")
                result[name] = ex
            else:
                result[f"example{i + 1}"] = {"value": ex}
        return result
    return {"example1": {"value": examples}}


def _collect_schemas(
    route: dict[str, Any], schemas: dict[str, Any]
) -> None:
    """Extract Pydantic model ``$defs`` into the shared components/schemas bucket."""
    # From route params (body models)
    for param in route.get("params", []):
        model_class = param.get("model_class")
        _collect_model_schemas(model_class, schemas)

    # From route.responses (extra status codes with models)
    for resp_info in (route.get("responses") or {}).values():
        if isinstance(resp_info, dict):
            _collect_model_schemas(resp_info.get("model"), schemas)

    # response_model
    _collect_model_schemas(route.get("response_model"), schemas)


def _collect_model_schemas(model_class, schemas: dict[str, Any]) -> None:
    """Extract $defs from a single Pydantic model."""
    if model_class is None:
        return
    if not hasattr(model_class, "model_json_schema"):
        return
    try:
        json_schema = model_class.model_json_schema()
    except Exception:
        return
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
    all_params = route.get("_all_params", route.get("params", []))
    for param in all_params:
        dep_callable = param.get("dep_callable")
        if dep_callable is None:
            dep_callable = param.get("_original_dep_callable")
        if dep_callable is None:
            continue

        obj = dep_callable
        if hasattr(obj, "model") and isinstance(obj.model, dict):
            scheme_name = getattr(obj, "scheme_name", None) or type(obj).__name__
            if scheme_name not in security_schemes:
                security_schemes[scheme_name] = obj.model


def _derive_security_from_deps(route: dict[str, Any]) -> list[dict[str, list[str]]]:
    """Auto-derive operation security list from detected security-scheme deps."""
    all_params = route.get("_all_params", route.get("params", []))
    out: list[dict[str, list[str]]] = []
    seen: set[str] = set()
    for param in all_params:
        dep_callable = param.get("dep_callable") or param.get("_original_dep_callable")
        if dep_callable is None:
            continue
        if hasattr(dep_callable, "model") and isinstance(dep_callable.model, dict):
            scheme_name = getattr(dep_callable, "scheme_name", None) or type(dep_callable).__name__
            if scheme_name in seen:
                continue
            seen.add(scheme_name)
            # Scopes for OAuth2 are the keys of the flow's scopes dict
            scopes: list[str] = []
            scheme_scopes = getattr(dep_callable, "scopes", None)
            if isinstance(scheme_scopes, dict):
                scopes = list(scheme_scopes.keys())
            out.append({scheme_name: scopes})
    return out


def _build_callbacks(callbacks: list) -> dict[str, Any]:
    """Render OpenAPI callbacks from a list of APIRouter instances.

    Each callback router becomes a nested entry under operation.callbacks:
        {router.name: {path: {method: operation}}}
    """
    from fastapi_rs.routing import APIRouter

    result: dict[str, Any] = {}
    for idx, cb in enumerate(callbacks):
        if not isinstance(cb, APIRouter):
            continue
        cb_name = getattr(cb, "name", None) or f"callback_{idx}"
        paths: dict[str, Any] = {}
        for cb_route in cb.routes:
            full_path = cb.prefix + cb_route.path
            methods_dict: dict[str, Any] = {}
            # Build a minimal route dict compatible with _build_operation
            cb_route_dict = {
                "path": full_path,
                "methods": cb_route.methods,
                "handler_name": cb_route.name,
                "params": [],
                "tags": cb_route.tags,
                "summary": cb_route.summary,
                "description": cb_route.description,
                "status_code": cb_route.status_code or 200,
                "response_description": cb_route.response_description,
                "responses": cb_route.responses,
                "deprecated": cb_route.deprecated,
                "operation_id": cb_route.operation_id,
                "include_in_schema": cb_route.include_in_schema,
                "openapi_extra": cb_route.openapi_extra,
            }
            for method in cb_route.methods:
                methods_dict[method.lower()] = _build_operation(cb_route_dict, method.lower())
            paths[full_path] = methods_dict
        result[cb_name] = paths
    return result


def _type_hint_to_schema(type_hint: str) -> dict[str, Any]:
    """Map a simple type-hint string to an OpenAPI schema fragment."""
    mapping: dict[str, dict[str, Any]] = {
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "str": {"type": "string"},
    }
    return dict(mapping.get(type_hint, {"type": "string"}))
