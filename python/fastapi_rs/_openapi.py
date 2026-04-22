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
    webhooks: list[dict[str, Any]] | None = None,
    external_docs: dict[str, Any] | None = None,
    summary: str | None = None,
    separate_input_output_schemas: bool = True,
) -> dict[str, Any]:
    """Generate an OpenAPI 3.1.0 schema dict from collected route metadata."""
    # Tolerate callers that pass Starlette/FA ``APIRoute`` objects directly
    # (``from fastapi.openapi.utils import get_openapi``; ``routes=app.routes``).
    # Convert each to the internal dict shape our generator expects.
    if routes:
        try:
            from fastapi_rs.routing import APIRoute as _APIRoute
            from fastapi_rs._introspect import introspect_endpoint as _intro
        except Exception:  # noqa: BLE001
            _APIRoute = None  # type: ignore[assignment]
            _intro = None  # type: ignore[assignment]
        if _APIRoute is not None and _intro is not None:
            _converted = []
            for r in routes:
                if isinstance(r, dict):
                    _converted.append(r)
                elif isinstance(r, _APIRoute):
                    try:
                        _params = _intro(r.endpoint, r.path)
                    except Exception:  # noqa: BLE001
                        _params = []
                    _converted.append({
                        "path": r.path,
                        "methods": list(r.methods) if r.methods else ["GET"],
                        "handler_name": r.name,
                        "is_async": False,
                        "params": _params,
                        "status_code": r.status_code or 200,
                        "summary": r.summary,
                        "description": r.description,
                        "response_description": getattr(
                            r, "response_description", "Successful Response"
                        ),
                        "responses": r.responses or {},
                        "response_model": getattr(r, "response_model", None),
                        "tags": r.tags or [],
                        "deprecated": r.deprecated or False,
                        "operation_id": getattr(r, "operation_id", None),
                        "include_in_schema": getattr(r, "include_in_schema", True),
                        "openapi_extra": getattr(r, "openapi_extra", None),
                    })
                # silently skip Starlette Mount / WebSocketRoute /
                # non-HTTP routes
            routes = _converted
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": version,
        },
        "paths": {},
    }

    if summary:
        schema["info"]["summary"] = summary
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

    if external_docs:
        schema["externalDocs"] = external_docs

    components_schemas: dict[str, Any] = {}
    security_schemes: dict[str, Any] = {}

    # Reset the per-build model-usage tracker. FastAPI's schema emitter is
    # called fresh for each build and decides the split on the current set
    # of routes; do the same.
    _MODEL_USAGE.clear()
    # Track whether split is requested so `_model_ref` and the post-pass
    # can honor ``separate_input_output_schemas=False`` (a single merged
    # schema per model, no ``-Input``/``-Output`` variants).
    global _SEPARATE_INPUT_OUTPUT
    _SEPARATE_INPUT_OUTPUT = separate_input_output_schemas

    # Pre-pass: walk every route to classify each Pydantic model as used
    # in "input" (body / form / file) and/or "output" (response_model,
    # user `responses[...].model`) contexts. The operation-building pass
    # then emits `<Name>-Input` / `<Name>-Output` refs accordingly. Also
    # register standalone Enum classes used as parameter types so we
    # can emit them under `components.schemas` like FastAPI does.
    enum_classes_seen: dict[str, type] = {}

    def _collect_enum_class(enum_cls):
        if enum_cls is None:
            return
        try:
            import enum as _enum_mod
            if isinstance(enum_cls, type) and issubclass(enum_cls, _enum_mod.Enum):
                name = enum_cls.__name__
                if name not in enum_classes_seen:
                    enum_classes_seen[name] = enum_cls
        except Exception:  # noqa: BLE001
            pass

    for route in routes:
        if not route.get("include_in_schema", True):
            continue
        for p in route.get("params", []):
            if p.get("kind") in ("body", "form", "file"):
                _note_model_usage(p.get("model_class"), "input")
            # Param-model-expansion fields carry the owning BaseModel on
            # ``_param_model_class``. Register its component schema too
            # (``Annotated[FormData, Form()]`` → ``FormData`` schema).
            _pmc = p.get("_param_model_class")
            if _pmc is not None and p.get("kind") in ("form", "file"):
                _note_model_usage(_pmc, "input")
            _collect_enum_class(p.get("enum_class"))
        _note_model_usage(route.get("response_model"), "output")
        for resp_info in (route.get("responses") or {}).values():
            if isinstance(resp_info, dict):
                _note_model_usage(resp_info.get("model"), "output")
    for wh in (webhooks or []):
        if not wh.get("include_in_schema", True):
            continue
        for p in wh.get("params", []):
            if p.get("kind") in ("body", "form", "file"):
                _note_model_usage(p.get("model_class"), "input")
        _note_model_usage(wh.get("response_model"), "output")

    # Track every Pydantic model class we encounter so the post-pass can
    # reconsider `-Input`/`-Output` splits for nested recursive models
    # (e.g. `Forest.roots: list[Node]` — Node only appears as a $defs
    # entry on Forest's schema, so the direct collect path can't split
    # it on its own).
    seen_model_classes: set[int] = set()
    model_class_by_id: dict[int, Any] = {}

    def _register_model(mc) -> None:
        if mc is None:
            return
        if not hasattr(mc, "model_json_schema"):
            # Python dataclasses are registered without Pydantic-specific
            # field walking — their fields can still be inspected for
            # nested BaseModel/dataclass refs.
            if _is_dataclass_type(mc):
                mid = id(mc)
                if mid not in seen_model_classes:
                    seen_model_classes.add(mid)
                    model_class_by_id[mid] = mc
                    try:
                        import dataclasses as _dc
                        for f in _dc.fields(mc):
                            for sub in _flatten_annotation_types(f.type):
                                _register_model(sub)
                    except Exception:  # noqa: BLE001
                        pass
            return
        mid = id(mc)
        if mid not in seen_model_classes:
            seen_model_classes.add(mid)
            model_class_by_id[mid] = mc
            for f in (getattr(mc, "model_fields", None) or {}).values():
                ann = getattr(f, "annotation", None)
                for sub in _flatten_annotation_types(ann):
                    _register_model(sub)

    _seen_operation_ids: dict[str, tuple[str, str]] = {}
    for route in routes:
        # Honor include_in_schema=False — skip route entirely
        if not route.get("include_in_schema", True):
            continue

        # Register every BaseModel reachable from this route so post-pass
        # has the concrete classes it needs for split emission. If the
        # param's ``model_class`` is a ``_TypeAdapterProxy`` (e.g. a
        # Union / generic wrapped for Pydantic), walk the inner
        # annotation so nested ``BaseModel`` arms are still emitted.
        for p in route.get("params", []):
            mc = p.get("model_class")
            _register_model(mc)
            _inner_ann = getattr(mc, "_annotation", None)
            if _inner_ann is not None:
                for sub in _flatten_annotation_types(_inner_ann):
                    _register_model(sub)
            # Param-model expansion keeps the owning BaseModel on
            # ``_param_model_class``. Only register for Form/File where
            # the schema IS emitted under components; Query/Header/
            # Cookie param-models are flattened into parameters only.
            _pmc = p.get("_param_model_class")
            if _pmc is not None and p.get("kind") in ("form", "file"):
                _register_model(_pmc)
        # For SSE routes, response_model carries the AsyncIterable[...]
        # form — register only the NON-SSE inner types. ServerSentEvent
        # itself is a transport wrapper and FA excludes it from
        # components.schemas.
        _is_sse_for_reg = False
        try:
            from fastapi_rs.responses import EventSourceResponse as _ESR_reg
            _rc_reg = route.get("response_class")
            if _rc_reg is not None and isinstance(_rc_reg, type) and issubclass(_rc_reg, _ESR_reg):
                _is_sse_for_reg = True
        except Exception:  # noqa: BLE001
            pass
        if not _is_sse_for_reg:
            _register_model(route.get("response_model"))
        for sub in _flatten_annotation_types(route.get("response_model")):
            if _is_sse_for_reg:
                try:
                    from fastapi_rs.sse import ServerSentEvent as _SSE_REG
                    if isinstance(sub, type) and issubclass(sub, _SSE_REG):
                        continue
                except Exception:  # noqa: BLE001
                    pass
            _register_model(sub)
        for resp_info in (route.get("responses") or {}).values():
            if isinstance(resp_info, dict):
                _register_model(resp_info.get("model"))
                for sub in _flatten_annotation_types(resp_info.get("model")):
                    _register_model(sub)
        # Callback routes carry their own response models that must be
        # emitted in components.schemas (FA parity for
        # ``callbacks=callback_router.routes``).
        for cb_model in _walk_callback_models(route.get("callbacks")):
            _register_model(cb_model)
            for sub in _flatten_annotation_types(cb_model):
                _register_model(sub)

        path = route["path"]
        # Starlette-style path converter suffixes like ``{file_path:path}``
        # are internal routing hints — OpenAPI exposes just the
        # parameter name. Strip ``:<converter>`` segments so the emitted
        # path matches FA's ``/files/{file_path}``.
        if ":" in path and "{" in path:
            import re as _re
            path = _re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)

        # Collect security schemes from route dependencies
        _collect_security_schemes(route, security_schemes)

        for method in route["methods"]:
            operation = _build_operation(route, method.lower())
            op_id = operation.get("operationId")
            # FA emits a UserWarning when two operations share an
            # operationId — tests rely on this (e.g.
            # ``test_include_router_defaults_overrides::test_openapi``).
            if op_id:
                existing = _seen_operation_ids.get(op_id)
                if existing is not None:
                    import warnings as _w
                    _w.warn(
                        f"Duplicate Operation ID {op_id} for function "
                        f"{route['handler_name']}",
                        stacklevel=1,
                    )
                else:
                    _seen_operation_ids[op_id] = (path, method)
            schema["paths"].setdefault(path, {})[method.lower()] = operation

            # Collect Pydantic model schemas into components
            _collect_schemas(route, components_schemas)

            # Hoist any synthesized Body_<handler> form/file schema into
            # components.schemas and replace the inline schema with a $ref
            # — matching FastAPI's output format (which always references a
            # component rather than inlining).
            _hoist_body_schema(operation, components_schemas)

    # Register enum classes into components.schemas so parameter refs
    # resolve to a shared `#/components/schemas/<EnumName>` entry,
    # matching FastAPI's layout.
    for enum_name, enum_cls in enum_classes_seen.items():
        if enum_name in components_schemas:
            continue
        values = [m.value for m in enum_cls]
        # int enums surface as `{type: integer}`, str-subclass ones as
        # `{type: string}`. Fall back to string when unclear.
        if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            inner_type = "integer"
        else:
            inner_type = "string"
        components_schemas[enum_name] = {
            "enum": values,
            "title": enum_name,
            "type": inner_type,
        }

    # Post-pass A: any self-recursive dual-use model that was collected
    # via `$defs` promotion (rather than as a direct `model_class`) still
    # needs to be split into `<Name>-Input` / `<Name>-Output`. Replace
    # the single `<Name>` entry with the two variants. For self-
    # referential models Pydantic returns `{"$defs": {"Name": <body>},
    # "$ref": "#/$defs/Name"}`; we want the `<body>` itself to live
    # under `components.schemas[Name-Input|Output]` with internal refs
    # pointing at the same suffixed entry so tools can resolve them
    # without dereferencing the pop'ed `Name`.
    split_models: set[str] = set()

    def _flatten_recursive_schema(ms, bare_name: str, variant_ref: str) -> dict[str, Any]:
        """Return the real `Name` body out of a Pydantic self-ref wrapper,
        with every inner `#/$defs/<bare_name>` pointer swapped to
        `#/components/schemas/<variant_ref>` so lookups land on the right
        split entry.
        """
        defs = (ms or {}).get("$defs") or {}
        body = defs.get(bare_name)
        if body is None:
            # Not a recursive wrapper — strip $defs and return.
            return {k: v for k, v in (ms or {}).items() if k != "$defs"}
        # Rewrite refs in the body to point at the variant target.
        target_ref = f"#/components/schemas/{variant_ref}"
        def _walk(v):
            if isinstance(v, dict):
                out = {}
                for k, vv in v.items():
                    if k == "$ref" and isinstance(vv, str) and vv.endswith(f"$defs/{bare_name}"):
                        out[k] = target_ref
                    else:
                        out[k] = _walk(vv)
                return out
            if isinstance(v, list):
                return [_walk(x) for x in v]
            return v
        return _walk(body)

    for mid, mc in model_class_by_id.items():
        usage = _MODEL_USAGE.get(mid, set())
        if not ("input" in usage and "output" in usage):
            continue
        # FA forces split whenever the model has a ``computed_field``
        # (val/ser shapes differ), even with
        # ``separate_input_output_schemas=False``.
        _has_cf = _model_has_computed_fields(mc)
        if not (separate_input_output_schemas or _has_cf):
            continue
        if not (_model_is_self_recursive(mc) or _val_ser_schemas_differ(mc)):
            continue
        raw = getattr(mc, "__name__", None)
        name = _normalize_model_name(raw) if raw else None
        if not name:
            continue
        try:
            v_schema = mc.model_json_schema(mode="validation")
            s_schema = mc.model_json_schema(mode="serialization")
        except Exception:  # noqa: BLE001
            continue
        components_schemas.pop(name, None)
        components_schemas[f"{name}-Input"] = _flatten_recursive_schema(v_schema, raw, f"{name}-Input")
        components_schemas[f"{name}-Output"] = _flatten_recursive_schema(s_schema, raw, f"{name}-Output")
        split_models.add(name)

    # Post-pass B: rewrite refs to split models in container schemas. A
    # container that's used only as output gets `#/components/schemas/X`
    # → `X-Output`; input-only gets `X-Input`. For mixed-usage containers
    # we leave the ref untouched (they'd have been split themselves if
    # they were self-recursive; non-recursive dual-use stays inline).
    def _rewrite_split_refs(tree, side: str):
        if isinstance(tree, dict):
            out = {}
            for k, vv in tree.items():
                if isinstance(vv, str):
                    for prefix in ("#/components/schemas/", "#/$defs/"):
                        if vv.startswith(prefix):
                            tail = vv[len(prefix):]
                            bare = _normalize_model_name(tail)
                            if bare in split_models:
                                out[k] = f"#/components/schemas/{bare}-{side}"
                                break
                    else:
                        out[k] = _rewrite_split_refs(vv, side)
                        continue
                    continue
                out[k] = _rewrite_split_refs(vv, side)
            return out
        if isinstance(tree, list):
            return [_rewrite_split_refs(x, side) for x in tree]
        return tree

    # Each container schema's context is determined by which model class
    # it belongs to, via _MODEL_USAGE.
    for mid, mc in list(model_class_by_id.items()):
        raw = getattr(mc, "__name__", None)
        name = _normalize_model_name(raw) if raw else None
        if not name or name in split_models or name not in components_schemas:
            continue
        usage = _MODEL_USAGE.get(mid, set())
        if usage == {"output"}:
            components_schemas[name] = _rewrite_split_refs(components_schemas[name], "Output")
        elif usage == {"input"}:
            components_schemas[name] = _rewrite_split_refs(components_schemas[name], "Input")

    # Rewrite split-model refs that appear inline inside path operations
    # (e.g. ``list[Item]`` response schema → ``items.$ref`` points at the
    # bare ``#/components/schemas/Item`` which is now ``Item-Output``).
    # Response bodies always map to ``-Output``, request bodies to
    # ``-Input``. Walk each operation and apply the direction-aware
    # rewriter.
    if split_models:
        _paths = schema.get("paths") or {}
        for _path_key, _path_item in _paths.items():
            if not isinstance(_path_item, dict):
                continue
            for _method, _op in _path_item.items():
                if not isinstance(_op, dict):
                    continue
                rb = _op.get("requestBody")
                if isinstance(rb, dict):
                    _op["requestBody"] = _rewrite_split_refs(rb, "Input")
                resps = _op.get("responses")
                if isinstance(resps, dict):
                    _op["responses"] = _rewrite_split_refs(resps, "Output")

        # Also propagate through the split variants themselves: each
        # ``<Name>-Input`` schema's nested refs point at other split
        # models' ``-Input`` variants, and ``-Output`` at ``-Output``.
        for _sname in list(components_schemas.keys()):
            if _sname.endswith("-Input"):
                components_schemas[_sname] = _rewrite_split_refs(
                    components_schemas[_sname], "Input"
                )
            elif _sname.endswith("-Output"):
                components_schemas[_sname] = _rewrite_split_refs(
                    components_schemas[_sname], "Output"
                )

    # Webhooks: top-level OpenAPI 3.1 field. Each webhook is effectively a
    # path-item object keyed by name rather than URL.
    if webhooks:
        wh_dict: dict[str, Any] = {}
        for wh in webhooks:
            if not wh.get("include_in_schema", True):
                continue
            name = wh.get("name") or wh["path"].lstrip("/")
            # Mark the route so ``_build_operation`` strips the leading
            # ``/`` when deriving operationId (FA treats webhook paths
            # as event names, not URLs).
            wh = {**wh, "_is_webhook": True}
            for method in wh["methods"]:
                op = _build_operation(wh, method.lower())
                wh_dict.setdefault(name, {})[method.lower()] = op
            _collect_security_schemes(wh, security_schemes)
            _collect_schemas(wh, components_schemas)
        if wh_dict:
            schema["webhooks"] = wh_dict

    # Check if any operation actually references ``HTTPValidationError``
    # (or ``ValidationError``) — FA only emits those component schemas
    # when used. User-overridden ``responses={422: {"model": CustomErr}}``
    # points at a different component, so we skip the default ones.
    def _uses_validation_ref(node) -> bool:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.endswith(
                ("/HTTPValidationError", "/ValidationError")
            ):
                return True
            return any(_uses_validation_ref(v) for v in node.values())
        if isinstance(node, list):
            return any(_uses_validation_ref(v) for v in node)
        return False
    _needs_validation_schemas = _uses_validation_ref(schema["paths"]) or _uses_validation_ref(schema.get("webhooks") or {})
    if _needs_validation_schemas:
        components_schemas.setdefault("ValidationError", {
            "title": "ValidationError",
            "type": "object",
            "required": ["loc", "msg", "type"],
            "properties": {
                "loc": {
                    "title": "Location",
                    "type": "array",
                    "items": {
                        "anyOf": [{"type": "string"}, {"type": "integer"}]
                    },
                },
                "msg": {"title": "Message", "type": "string"},
                "type": {"title": "Error Type", "type": "string"},
                # Pydantic v2 includes `input` (the offending value the
                # validator saw) and `ctx` (the constraint metadata, e.g.
                # `{"ge": 0}`). FastAPI advertises both in its OpenAPI so
                # downstream codegen tools can type the detail payload.
                "input": {"title": "Input"},
                "ctx": {"title": "Context", "type": "object"},
            },
        })
        components_schemas.setdefault("HTTPValidationError", {
            "title": "HTTPValidationError",
            "type": "object",
            "properties": {
                "detail": {
                    "title": "Detail",
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/ValidationError"},
                }
            },
        })

    # FA strips the private part of a model docstring after the first
    # ``\f`` (formfeed) — ``"Public\fPrivate"`` → ``"Public"``. Apply the
    # same truncation to any ``description`` in components.schemas.
    def _strip_formfeed(obj: Any) -> None:
        if isinstance(obj, dict):
            _d = obj.get("description")
            if isinstance(_d, str) and "\f" in _d:
                obj["description"] = _d.split("\f", 1)[0]
            for v in obj.values():
                _strip_formfeed(v)
        elif isinstance(obj, list):
            for v in obj:
                _strip_formfeed(v)
    for _sname in list(components_schemas.keys()):
        _strip_formfeed(components_schemas[_sname])

    components: dict[str, Any] = {}
    if components_schemas:
        components["schemas"] = components_schemas
    if security_schemes:
        components["securitySchemes"] = security_schemes
    if components:
        schema["components"] = components

    # Final pass: hoist inline Pydantic `$defs` blocks up to the shared
    # `components.schemas` bucket. This mutates `schema` in place. The
    # subsequent `_rewrite_defs_refs` traverses `schema` (which now holds
    # the merged components) rewriting `$ref: #/$defs/X` →
    # `#/components/schemas/X` and dropping Pydantic-emitted `default:
    # null` leaves that FastAPI strips from its own generated spec.
    if components_schemas:
        schema.setdefault("components", {})["schemas"] = components_schemas
    _hoist_inline_defs(schema, components_schemas, skip=split_models)
    schema = _rewrite_defs_refs(schema)

    return schema


def _hoist_inline_defs(node: Any, bucket: dict[str, Any], skip: set[str] | None = None) -> None:
    """Walk a schema tree, move any `$defs` entries into `bucket`, and
    delete them in place. Subsequent `_rewrite_defs_refs` re-points
    `$ref: #/$defs/X` at the hoisted component.

    ``skip`` is a set of model names that were split into
    ``<Name>-Input`` / ``<Name>-Output`` variants — those must NOT be
    re-added to the components bucket under the plain name.
    """
    if isinstance(node, dict):
        if "$defs" in node:
            defs = node.pop("$defs")
            if isinstance(defs, dict):
                for name, defn in defs.items():
                    if skip and name in skip:
                        continue
                    bucket.setdefault(name, defn)
        for v in node.values():
            _hoist_inline_defs(v, bucket, skip)
    elif isinstance(node, list):
        for v in node:
            _hoist_inline_defs(v, bucket, skip)


def _build_operation(route: dict[str, Any], method: str) -> dict[str, Any]:
    """Build an OpenAPI operation object for a single route+method."""
    status_code = route.get("status_code")
    # The route builder defaults ``status_code`` to 200, but if the
    # response_class has no media_type (e.g. RedirectResponse), FA
    # uses the response class's ``__init__`` default instead. We only
    # need to override in the 200 case because user-provided
    # ``status_code=200`` and a RedirectResponse is an odd combination
    # that FA likewise inspects and still falls through to 307.
    if status_code in (None, 200):
        _rc = route.get("response_class")
        if _rc is not None and getattr(_rc, "media_type", None) is None:
            try:
                import inspect as _inspect
                _sig = _inspect.signature(_rc.__init__)
                _sc_p = _sig.parameters.get("status_code")
                if (
                    _sc_p is not None
                    and isinstance(_sc_p.default, int)
                    and _sc_p.default != 200
                ):
                    status_code = _sc_p.default
            except (TypeError, ValueError):
                pass
    if status_code is None:
        status_code = 200

    # Pre-compute the operation_id here so response-schema titling can
    # reference it. FA's ``Response <Title-cased Operation Id>`` uses
    # the auto-generated ``<name>_<path>_<method>`` form.
    if route.get("operation_id"):
        _op_id_for_title = route["operation_id"]
    else:
        import re as _re
        # Strip Starlette ``:<converter>`` suffixes from the path before
        # building the operationId (FA never sees the converter hint).
        _op_path = route.get("path", "")
        _op_path = _re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", _op_path)
        # Webhooks: FA stores webhook paths WITHOUT a leading ``/``
        # (they're event names, not URLs). Our collector prepends ``/``
        # for consistency with regular routes, but the operationId
        # derivation needs the FA-shaped form — strip the leading ``/``
        # for webhook routes.
        if route.get("_is_webhook"):
            _op_path = _op_path.lstrip("/")
        _op_id_for_title = f"{route.get('handler_name', '')}{_op_path}"
        _op_id_for_title = _re.sub(r"\W", "_", _op_id_for_title)
        _op_id_for_title = f"{_op_id_for_title}_{method.lower()}"
    route = {**route, "operation_id": _op_id_for_title}
    response_desc = route.get("response_description") or "Successful Response"

    # Success response skeleton — overridable by route.responses[status]
    # Use $ref for response_model if it's a Pydantic model. For generic
    # aliases (`dict[str, X]`, `list[X]`, `Optional[X]`), go through a
    # Pydantic TypeAdapter so the generated schema matches FastAPI exactly.
    response_model = route.get("response_model")
    response_schema: dict[str, Any] = {}
    if response_model is not None:
        ref = _model_ref(response_model, mode="serialization")
        if ref is not None:
            response_schema = ref
        elif hasattr(response_model, "model_json_schema"):
            try:
                response_schema = response_model.model_json_schema(mode="serialization")
            except Exception:
                pass
        else:
            try:
                from pydantic import TypeAdapter
                response_schema = TypeAdapter(response_model).json_schema(mode="serialization")
                # FastAPI labels the generated success schema with the
                # operation title (`Response Root Index`).
                if "title" not in response_schema:
                    # FA uses ``Response <Title-cased Operation Id>``;
                    # fall back to handler name when no operationId.
                    source = (
                        route.get("operation_id")
                        or route.get("handler_name")
                        or "response"
                    )
                    # FA preserves empty segments from double-underscore
                    # splits (``items__get`` → ``Items  Get`` with two
                    # spaces) AND preserves hyphens in custom op_ids
                    # (``items-get_items`` → ``Items-Get Items``). Split
                    # on ``_`` first, then capitalize each chunk while
                    # preserving the hyphen in token-level splits.
                    def _title_segment(tok: str) -> str:
                        if not tok:
                            return ""
                        if "-" in tok:
                            return "-".join(
                                w.capitalize() for w in tok.split("-")
                            )
                        return tok.capitalize()
                    response_schema["title"] = "Response " + " ".join(
                        _title_segment(w) for w in source.split("_")
                    )
            except Exception:
                pass
    # Choose the response media type from the configured response_class.
    # HTMLResponse → text/html, PlainTextResponse → text/plain, etc.
    _resp_cls = route.get("response_class")
    _media_type: str | None = "application/json"
    _suppress_content = False
    if _resp_cls is not None:
        _declared_mt = getattr(_resp_cls, "media_type", None)
        if _declared_mt is None:
            # media_type=None response classes (RedirectResponse, plain
            # Response(), etc.) emit no content in OpenAPI per FA's
            # ``route_response_media_type`` guard in openapi/utils.py.
            _suppress_content = True
            _media_type = None
        else:
            _media_type = _declared_mt
    # For plain-text response classes, FA emits ``{type: string}`` as
    # the schema (raw text, no Pydantic serialization). For custom
    # JSON-like classes (``application/vnd.api+json``, etc.) and JSON,
    # FA keeps whatever the response_model produced — or an empty
    # ``{}`` when none was declared.
    if _media_type in ("text/html", "text/plain"):
        response_schema = {"type": "string"}
    elif _media_type is not None and _media_type != "application/json":
        # Custom ``response_class=StreamingResponse`` (or subclass) with a
        # non-JSON media_type — FA emits ``{"type": "string"}`` to
        # represent the raw byte / text body. Overrides any auto-derived
        # response_model (which is typically ``AsyncIterable[bytes]`` for
        # a streaming endpoint).
        try:
            from fastapi_rs.responses import StreamingResponse as _SR
            _rc_cls = route.get("response_class")
            if _rc_cls is not None and isinstance(_rc_cls, type) and issubclass(_rc_cls, _SR):
                response_schema = {"type": "string"}
            elif not route.get("response_model"):
                response_schema = {}
        except Exception:  # noqa: BLE001
            if not route.get("response_model"):
                response_schema = {}
    # SSE (text/event-stream) + generator endpoint → FA emits an
    # ``itemSchema`` object describing the per-event structure (data,
    # event, id, retry), with ``contentSchema`` pointing at the yielded
    # item type when the handler annotates it.
    _is_sse = False
    _is_jsonl = False
    try:
        from fastapi_rs.responses import EventSourceResponse as _ESR
        _rc_cls = route.get("response_class")
        if _rc_cls is not None and isinstance(_rc_cls, type) and issubclass(_rc_cls, _ESR):
            _is_sse = True
    except Exception:  # noqa: BLE001
        pass
    # JSONL detection: generator endpoint with no explicit SSE response
    # class — FA emits ``application/jsonl`` with an ``itemSchema`` (the
    # inner yielded type when annotated, empty ``{}`` when not).
    if not _is_sse and not route.get("response_class"):
        _ret_ann = route.get("response_model")
        if _ret_ann is not None:
            try:
                import typing as _typing
                from collections.abc import AsyncIterable as _AI, Iterable as _I
                _orig = _typing.get_origin(_ret_ann)
                if _orig is _AI or _orig is _I:
                    _is_jsonl = True
            except Exception:  # noqa: BLE001
                pass
        # Un-annotated generator endpoints (``def f(): yield ...``) also
        # stream as JSONL. Our ``applications._collect_routes_from_router``
        # wraps them in a closure named ``_json_lines_wrap`` — use the
        # wrapper name as the detection signal.
        if not _is_jsonl:
            _ep = route.get("endpoint")
            if _ep is not None and getattr(_ep, "__name__", "") == "_json_lines_wrap":
                _is_jsonl = True

    if _suppress_content:
        success_response = {"description": response_desc}
    elif _is_jsonl:
        # JSONL (application/jsonl) — FA emits ``itemSchema`` pointing at
        # the inner yielded type when annotated, or empty ``{}`` when
        # the handler is un-annotated.
        jsonl_item: dict[str, Any] = {}
        _ret_ann_jsonl = route.get("response_model")
        _inner_jsonl = None
        try:
            import typing as _typing_jsonl
            _args_jsonl = _typing_jsonl.get_args(_ret_ann_jsonl)
            if _args_jsonl:
                _inner_jsonl = _args_jsonl[0]
        except Exception:  # noqa: BLE001
            _inner_jsonl = None
        if _inner_jsonl is not None:
            _ref_jsonl = _model_ref(_inner_jsonl, mode="serialization")
            if _ref_jsonl is not None:
                jsonl_item = _ref_jsonl
            else:
                try:
                    from pydantic import TypeAdapter as _TA_jsonl
                    _sch_jsonl = _TA_jsonl(_inner_jsonl).json_schema(mode="serialization")
                    if "$defs" in _sch_jsonl:
                        _sch_jsonl = {k: v for k, v in _sch_jsonl.items() if k != "$defs"}
                    jsonl_item = _sch_jsonl
                except Exception:  # noqa: BLE001
                    jsonl_item = {}
        success_response = {
            "description": response_desc,
            "content": {"application/jsonl": {"itemSchema": jsonl_item}},
        }
    elif _is_sse:
        # Item schema for SSE events. ``data`` may carry a JSON-encoded
        # payload (``contentMediaType: application/json`` +
        # ``contentSchema: <item>``), or be a plain string when the
        # handler has no return annotation.
        item_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "data": {"type": "string"},
                "event": {"type": "string"},
                "id": {"type": "string"},
                "retry": {"type": "integer", "minimum": 0},
            },
        }
        # Try to pull the inner item type from the handler's return
        # annotation (``AsyncIterable[Item]`` → ``Item``). FA walks
        # this when building ``contentSchema``. For SSE endpoints the
        # ``response_model`` is set to the AsyncIterable/Iterable form,
        # so we unwrap it here.
        _ret = route.get("response_model")
        _inner = None
        try:
            import typing as _typing
            _origin = _typing.get_origin(_ret)
            if _origin is not None:
                _args = _typing.get_args(_ret)
                if _args:
                    _inner = _args[0]
            elif isinstance(_ret, type) and hasattr(_ret, "model_json_schema"):
                _inner = _ret
        except Exception:  # noqa: BLE001
            _inner = None
        # FA excludes ``ServerSentEvent`` itself as the inner type — it's
        # a transport wrapper, not a data model, so no ``contentSchema``.
        try:
            from fastapi_rs.sse import ServerSentEvent as _SSE_CLS
            if isinstance(_inner, type) and issubclass(_inner, _SSE_CLS):
                _inner = None
        except Exception:  # noqa: BLE001
            pass
        if _inner is not None:
            content_schema = _model_ref(_inner, mode="serialization")
            if content_schema is None:
                try:
                    from pydantic import TypeAdapter as _TA
                    content_schema = _TA(_inner).json_schema(mode="serialization")
                    if "$defs" in content_schema:
                        content_schema = {k: v for k, v in content_schema.items() if k != "$defs"}
                except Exception:  # noqa: BLE001
                    content_schema = None
            if content_schema is not None:
                item_schema["required"] = ["data"]
                item_schema["properties"]["data"] = {
                    "type": "string",
                    "contentMediaType": "application/json",
                    "contentSchema": content_schema,
                }
        success_response = {
            "description": response_desc,
            "content": {"text/event-stream": {"itemSchema": item_schema}},
        }
    else:
        success_response = {
            "description": response_desc,
            "content": {_media_type: {"schema": response_schema}},
        }

    # Build the full responses dict, starting with user-supplied responses
    # merged with the auto-generated success entry
    responses_dict: dict[str, Any] = {}
    user_responses = route.get("responses") or {}
    for status_key, resp_info in user_responses.items():
        # Normalise range-code keys: ``"5xx"`` → ``"5XX"`` (FA does this
        # before emitting to OpenAPI). Int codes stay numeric.
        _raw = str(status_key)
        _normalised: str
        _lo = _raw.lower()
        if _lo in ("1xx", "2xx", "3xx", "4xx", "5xx"):
            _normalised = _raw.upper()
        elif _lo == "default":
            _normalised = "default"
        else:
            # Validate numeric status code — FA raises ValueError for
            # non-numeric keys that aren't range codes or "default".
            try:
                int(_raw)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Invalid status code: {status_key!r}. "
                    "Must be an int, a range code like '5XX', or 'default'."
                )
            _normalised = _raw
        # Custom response_class with non-JSON media_type (e.g.
        # ``JsonApiResponse`` → ``application/vnd.api+json``) — per
        # FA, the additional responses from ``responses={...}`` inherit
        # that media type when they supply a model but no explicit
        # ``content``.
        _default_mt = _media_type if isinstance(_media_type, str) else "application/json"
        entry = _build_response_entry(
            resp_info, status_code=_normalised, default_media_type=_default_mt
        )
        # FA titles generated response schemas ``Response <StatusCode>
        # <Title-cased Operation Id>``. Apply the title if the schema
        # we produced didn't come from a named component (pure
        # container like list[Message] returns an inline schema).
        content = entry.get("content")
        if isinstance(content, dict):
            for mt, mobj in content.items():
                sch = mobj.get("schema") if isinstance(mobj, dict) else None
                if isinstance(sch, dict) and "$ref" not in sch and "title" not in sch:
                    op_source = route.get("operation_id") or route.get("handler_name") or ""
                    op_title = " ".join(
                        w.capitalize() for w in op_source.replace("-", "_").split("_") if w
                    )
                    try:
                        status_txt = str(int(status_key))
                    except (TypeError, ValueError):
                        status_txt = str(status_key)
                    if op_title:
                        sch["title"] = f"Response {status_txt} {op_title}"
        responses_dict[_normalised] = entry

    # Always include success response (route.responses may override it).
    # FastAPI's exact behavior: user-supplied entry at the success code
    # is merged with the auto-generated entry — the user can override
    # `description`, but the `content` schema from `response_model` is
    # still added if the user didn't supply one. Without this, a route
    # with `responses={200: {"description": "OK"}}` loses its entire
    # response schema.
    success_key = str(status_code)
    if success_key not in responses_dict:
        responses_dict[success_key] = success_response
    else:
        responses_dict[success_key].setdefault("description", response_desc)
        existing_content = responses_dict[success_key].get("content")
        auto_content = success_response.get("content") or {}
        if not existing_content:
            responses_dict[success_key]["content"] = auto_content
        elif auto_content:
            # FA merges: keys present in user's content take precedence;
            # keys only in auto_content (typically application/json from
            # response_model) are added. Additionally, for media types
            # present in BOTH, schema from auto_content is merged in when
            # the user didn't supply one (so ``example`` + response_model
            # still produces a ``$ref``-ed schema).
            merged: dict[str, Any] = {}
            for mt, obj in auto_content.items():
                if mt not in existing_content:
                    merged[mt] = obj
            for mt, obj in existing_content.items():
                if isinstance(obj, dict) and mt in auto_content:
                    auto_obj = auto_content[mt]
                    if isinstance(auto_obj, dict) and "schema" not in obj and "schema" in auto_obj:
                        obj = {"schema": auto_obj["schema"], **obj}
                merged[mt] = obj
            responses_dict[success_key]["content"] = merged

    # Auto-add 422 Validation Error response. FastAPI's rule
    # (``get_flat_params(dependant)`` in openapi/utils.py): 422 fires
    # whenever the route has any user-declared parameter OR a body
    # field. Framework-injected security dependencies (APIKeyHeader,
    # APIKeyCookie, APIKeyQuery, HTTPBearer, OAuth2*) don't appear
    # in that list because they read ``request.headers`` etc. directly
    # — they never surface a 422, they return 401/403. We match by
    # only counting params that are user-visible (``_is_handler_param``)
    # AND have a validator, a constraint, or are a body-side type.
    def _param_can_fail_validation(p: dict[str, Any]) -> bool:
        kind = p.get("kind")
        if kind in ("body", "form", "file"):
            return True
        if kind == "path":
            # Path params always coerce to type_hint; mismatched type → 422.
            return True
        if kind in ("query", "header", "cookie"):
            # A user-level Query/Header/Cookie — the type itself is
            # enough to generate 422 when the handler declares it.
            # FA's ``get_flat_params`` includes these even when they
            # have a default, because supplying ``?n=abc`` where
            # ``n: int | None = None`` still produces a 422.
            return True
        return False

    # Collect names of extraction steps that feed a Starlette security
    # scheme — those shouldn't count toward 422 (APIKeyHeader reads the
    # header directly; missing/malformed → 401/403, not 422).
    _sec_sub_names: set[str] = set()
    for _p in route.get("_all_params", route.get("params", [])):
        if _p.get("kind") != "dependency":
            continue
        _dc = _p.get("_original_dep_callable") or _p.get("dep_callable")
        if _dc is not None and hasattr(_dc, "model") and isinstance(_dc.model, dict):
            for _, _sk in _p.get("dep_input_map") or []:
                if isinstance(_sk, str):
                    _sec_sub_names.add(_sk)

    # FA emits 422 even when the param is ``include_in_schema=False`` —
    # the hidden param can still fail validation; the error response
    # just references HTTPValidationError (the hidden param doesn't
    # appear in ``parameters``).
    has_validated_params = any(
        _param_can_fail_validation(p)
        for p in route.get("params", [])
        if p.get("_is_handler_param", False)
        and p.get("name") not in _sec_sub_names
    )
    if has_validated_params and "422" not in responses_dict:
        responses_dict["422"] = {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/HTTPValidationError",
                    }
                }
            },
        }

    # Drop the auto-generated `content` block for empty responses (204 /
    # 304 etc.) — FastAPI omits `content` for those status codes because
    # the HTTP spec forbids a body. Without this we emit
    # `{"application/json": {"schema": {}}}` which is harmless but diffs.
    for _empty_code in ("204", "304"):
        if _empty_code in responses_dict and isinstance(responses_dict[_empty_code], dict):
            responses_dict[_empty_code].pop("content", None)

    # FastAPI auto-titles the handler's function name (`root_index` →
    # `"Root Index"`) when the user didn't set an explicit `summary=`. Match
    # that so downstream OpenAPI consumers see the same operation label.
    _auto_summary = route.get("summary")
    if not _auto_summary:
        _auto_summary = " ".join(
            w.capitalize() for w in route["handler_name"].replace("-", "_").split("_") if w
        )
    if route.get("operation_id"):
        _op_id = route["operation_id"]
    else:
        # Mirror FA's ``generate_unique_id``: name + path (non-word
        # chars → underscore) + method lowercased. So route
        # ``@app.post("/foo") def foo()`` → ``foo_foo_post``.
        import re as _re
        _op_id = f"{route['handler_name']}{route['path']}"
        _op_id = _re.sub(r"\W", "_", _op_id)
        _op_id = f"{_op_id}_{method}"
    operation: dict[str, Any] = {
        "summary": _auto_summary,
        "operationId": _op_id,
        "responses": responses_dict,
    }

    if route.get("tags"):
        # FA allows Enum values (``tags=[Tags.items]``) — emit the
        # string value rather than the Enum instance so JSON
        # serialization succeeds.
        import enum as _enum
        operation["tags"] = [
            t.value if isinstance(t, _enum.Enum) else t for t in route["tags"]
        ]
    if route.get("description"):
        operation["description"] = route["description"]
    if route.get("deprecated"):
        operation["deprecated"] = True

    # Parameters (path, query, header, cookie) and request body
    parameters: list[dict[str, Any]] = []
    request_body: dict[str, Any] | None = None
    form_params: list[dict[str, Any]] = []
    file_params: list[dict[str, Any]] = []

    # Collect names of params that came from Security() dep resolution —
    # FastAPI surfaces those only under `operation.security`, never in
    # `parameters`. A dep whose callable has a `.model` attribute is a
    # Starlette security scheme (APIKeyHeader / OAuth2PasswordBearer /
    # HTTPBearer etc.); its listed sub-param names are the extraction
    # steps we need to hide.
    security_sub_param_names: set[str] = set()
    for p in route.get("_all_params", route.get("params", [])):
        if p.get("kind") != "dependency":
            continue
        dep_callable = p.get("_original_dep_callable") or p.get("dep_callable")
        if dep_callable is None:
            continue
        if hasattr(dep_callable, "model") and isinstance(dep_callable.model, dict):
            for _, source_key in p.get("dep_input_map") or []:
                if isinstance(source_key, str):
                    security_sub_param_names.add(source_key)

    # FA groups parameters by kind before emitting: path → query →
    # header → cookie. Body / form / file remain in declaration order
    # (they don't go in ``parameters``, so we process them in their
    # original slot).
    _kind_order = {"path": 0, "query": 1, "header": 2, "cookie": 3}

    def _param_sort_key(_p):
        _k = _p.get("kind")
        if _k in _kind_order:
            return (0, _kind_order[_k])
        return (1, 0)  # body/form/file/dependency — original order

    _route_params_sorted = sorted(
        enumerate(route.get("params", [])),
        key=lambda _idx_p: (_param_sort_key(_idx_p[1]), _idx_p[0]),
    )
    for _, param in _route_params_sorted:
        # Honor param-level include_in_schema
        if not param.get("include_in_schema", True):
            continue
        if param.get("name") in security_sub_param_names:
            continue
        kind = param.get("kind", "")
        if kind in ("path", "query", "header", "cookie"):
            parameters.append(_build_parameter(param))
        elif kind == "body":
            request_body = _build_request_body(param)
        elif kind == "form":
            form_params.append(param)
        elif kind == "file":
            file_params.append(param)
        # Skip "dependency" params — they are internal

    # Form / File params share a single `Body_<handler>` component and live
    # under `requestBody.content[multipart/form-data | application/x-www-
    # form-urlencoded].schema`. Matches FastAPI's generated schema so
    # OpenAPI consumers see the same canonical body model name.
    # Keep declaration order of the handler signature — FastAPI emits the
    # properties in the exact order the user defined them.
    if request_body is None and (form_params or file_params):
        ordered_mixed: list[tuple[str, dict[str, Any]]] = []  # ("form" | "file", param)
        for p in route.get("params", []):
            if not p.get("include_in_schema", True):
                continue
            k = p.get("kind")
            if k == "form":
                ordered_mixed.append(("form", p))
            elif k == "file":
                ordered_mixed.append(("file", p))
        request_body = _build_form_file_body(route, form_params, file_params, ordered_mixed)

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

    # Per-operation servers (OpenAPI 3.1)
    servers = route.get("servers")
    if servers:
        operation["servers"] = servers

    # Per-operation externalDocs (OpenAPI 3.1)
    external_docs = route.get("external_docs")
    if external_docs:
        operation["externalDocs"] = external_docs

    # Merge in openapi_extra (user's custom OpenAPI fields). For the
    # ``parameters`` key specifically, FA extends the existing list
    # rather than replacing it — so the user's openapi_extra parameters
    # add to the handler's auto-derived ones.
    openapi_extra = route.get("openapi_extra") or {}
    if openapi_extra:
        for k, v in openapi_extra.items():
            if k == "parameters" and isinstance(v, list):
                existing = operation.get("parameters") or []
                operation["parameters"] = list(existing) + list(v)
            elif k == "responses" and isinstance(v, dict):
                existing_r = operation.get("responses") or {}
                merged: dict[str, Any] = dict(existing_r)
                for sk, sv in v.items():
                    if sk in merged and isinstance(merged[sk], dict) and isinstance(sv, dict):
                        merged[sk] = {**merged[sk], **sv}
                    else:
                        merged[sk] = sv
                operation["responses"] = merged
            else:
                operation[k] = v

    return operation


_DEFAULT_STATUS_DESCRIPTIONS = {
    100: "Continue", 101: "Switching Protocols", 102: "Processing",
    200: "Successful Response", 201: "Created", 202: "Accepted",
    203: "Non-Authoritative Information", 204: "No Content", 205: "Reset Content",
    206: "Partial Content", 207: "Multi-Status", 208: "Already Reported",
    226: "IM Used",
    300: "Multiple Choices", 301: "Moved Permanently", 302: "Found",
    303: "See Other", 304: "Not Modified", 305: "Use Proxy", 307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request", 401: "Unauthorized", 402: "Payment Required",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    406: "Not Acceptable", 407: "Proxy Authentication Required", 408: "Request Timeout",
    409: "Conflict", 410: "Gone", 411: "Length Required",
    412: "Precondition Failed", 413: "Content Too Large", 414: "URI Too Long",
    415: "Unsupported Media Type", 416: "Range Not Satisfiable", 417: "Expectation Failed",
    418: "I'm a teapot", 421: "Misdirected Request", 422: "Unprocessable Entity",
    423: "Locked", 424: "Failed Dependency", 425: "Too Early", 426: "Upgrade Required",
    428: "Precondition Required", 429: "Too Many Requests",
    431: "Request Header Fields Too Large", 451: "Unavailable For Legal Reasons",
    500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
    505: "HTTP Version Not Supported", 506: "Variant Also Negotiates",
    507: "Insufficient Storage", 508: "Loop Detected",
    510: "Not Extended", 511: "Network Authentication Required",
}


_RANGE_STATUS_DESCRIPTIONS = {
    "1XX": "Information",
    "2XX": "Success",
    "3XX": "Redirection",
    "4XX": "Client Error",
    "5XX": "Server Error",
    "default": "Default Response",
}


def _build_response_entry(
    resp_info: dict[str, Any],
    status_code: int | str | None = None,
    op_title: str | None = None,
    default_media_type: str = "application/json",
) -> dict[str, Any]:
    """Convert a user-supplied entry from route.responses into an OpenAPI response object.

    Accepts forms like:
        {"description": "Not found"}
        {"description": "Error", "model": MyError}
        {"description": "Error", "model": list[MyError]}
        {"description": "X", "content": {"application/json": {"schema": {...}}}}
    """
    entry: dict[str, Any] = {}
    # Match FA: HTTP status codes get the canonical reason phrase when
    # the user didn't set an explicit description (``404`` → ``Not Found``;
    # ``"5XX"`` → ``"Server Error"``; ``"default"`` → ``"Default Response"``).
    default_desc = "Response"
    try:
        if status_code is not None:
            raw_key = str(status_code)
            # ``"5xx"`` / ``"4XX"`` both normalise to the upper form.
            range_key = raw_key.upper()
            if raw_key == "default":
                default_desc = _RANGE_STATUS_DESCRIPTIONS["default"]
            elif range_key in _RANGE_STATUS_DESCRIPTIONS:
                default_desc = _RANGE_STATUS_DESCRIPTIONS[range_key]
            else:
                status_int = int(status_code)
                default_desc = _DEFAULT_STATUS_DESCRIPTIONS.get(status_int, default_desc)
    except (TypeError, ValueError):
        pass
    entry["description"] = resp_info.get("description", default_desc)

    # If model is provided, build content automatically.
    model = resp_info.get("model")
    if model is not None:
        ref = _model_ref(model)
        if ref is not None:
            entry["content"] = {default_media_type: {"schema": ref}}
        elif hasattr(model, "model_json_schema"):
            try:
                model_schema = model.model_json_schema()
                entry["content"] = {default_media_type: {"schema": model_schema}}
            except Exception:
                pass
        else:
            # Container-typed responses (``list[MyError]`` /
            # ``dict[str, Model]`` / Union etc.). Use a TypeAdapter to
            # produce the inner schema and rewrite any ``#/$defs/`` refs
            # to components-relative — matches FA's emission.
            try:
                from pydantic import TypeAdapter as _TA
                container_schema = _TA(model).json_schema(mode="validation")
                # Flatten nested $defs — we rely on _register_model to
                # have already surfaced referenced BaseModel arms to
                # ``components.schemas``.
                if "$defs" in container_schema:
                    container_schema = {
                        k: v for k, v in container_schema.items() if k != "$defs"
                    }
                container_schema = _rewrite_defs_refs(container_schema)
                entry["content"] = {default_media_type: {"schema": container_schema}}
            except Exception:  # noqa: BLE001
                pass

    # Direct content/headers overrides. If the user supplies ``content``
    # AND a ``model`` was provided, FA merges: user's content takes
    # precedence, but the model-derived schema is added for media types
    # where the user only supplied metadata (examples/example) without
    # their own schema.
    if "content" in resp_info:
        user_content = resp_info["content"]
        auto_content = entry.get("content") or {}
        if auto_content and isinstance(user_content, dict):
            merged: dict[str, Any] = {}
            for mt, obj in auto_content.items():
                if mt not in user_content:
                    merged[mt] = obj
            for mt, obj in user_content.items():
                if isinstance(obj, dict) and mt in auto_content:
                    auto_obj = auto_content[mt]
                    if isinstance(auto_obj, dict) and "schema" not in obj and "schema" in auto_obj:
                        obj = {"schema": auto_obj["schema"], **obj}
                merged[mt] = obj
            entry["content"] = merged
        else:
            entry["content"] = user_content
    if "headers" in resp_info:
        entry["headers"] = resp_info["headers"]
    if "links" in resp_info:
        entry["links"] = resp_info["links"]

    return entry


def _build_parameter(param: dict[str, Any]) -> dict[str, Any]:
    """Build an OpenAPI parameter object (path/query/header/cookie).

    FastAPI populates `schema.title` automatically from the parameter name
    (`offset` → `"Offset"`) and pushes `description` + numeric/length
    constraints onto the schema too. Matching that keeps us byte-identical
    in the generated openapi.json.
    """
    # For parameter-model synthetic fields (FA 0.115+ feature where
    # ``Annotated[BaseModel, Query()]`` expands into individual query
    # params) the OpenAPI name is the model's FIELD name, not the
    # internal ``__pm_<owner>__<field>`` bookkeeping key, and the
    # schema type must come from the FIELD annotation (``list[str]``
    # → ``type: array``) rather than the wire runtime-coercion hint.
    pm_field_name = param.get("_param_model_field_name")
    if pm_field_name is not None:
        p_name = param.get("alias") or pm_field_name
        _schema_type_hint = param.get("_param_model_schema_type_hint") or "str"
        _required_for_schema = param.get("_param_model_field_info").is_required() if param.get("_param_model_field_info") else False
    else:
        p_name = param.get("alias") or param["name"]
        _schema_type_hint = param.get("type_hint", "str")
        _required_for_schema = param["required"]
    # Prefer a richer inline schema when we recognize the (unwrapped)
    # annotation (UUID, datetime, date, etc.). Falls back to the plain
    # type-hint mapping. For `list[T]` we dig into the inner annotation
    # so `list[UUID]` picks up `items.format = uuid`.
    ua = param.get("_unwrapped_annotation")
    inner_schema = None
    list_inner_ann = None
    _bare_list = False
    _annotation_absent = ua is None and pm_field_name is None
    import typing as _typing
    _is_unique_container = False
    if ua is not None:
        # Peel list containers to get the item annotation.
        origin = _typing.get_origin(ua)
        if origin in (list, tuple, set, frozenset):
            args = _typing.get_args(ua)
            if args:
                list_inner_ann = args[0]
            if origin in (set, frozenset):
                _is_unique_container = True
        elif ua in (list, tuple, set, frozenset):
            # Bare ``list`` / ``tuple`` / ``set`` — FA emits ``items: {}``.
            _bare_list = True
            if ua in (set, frozenset):
                _is_unique_container = True
        else:
            inner_schema = _schema_for_annotation(ua)
    if inner_schema is None:
        if _bare_list:
            inner_schema = {"type": "array", "items": {}}
        elif _annotation_absent:
            # Unannotated parameter (e.g. ``def f(item_id):``). FA emits a
            # schema with just ``title`` and no ``type`` in this case.
            inner_schema = {}
        else:
            inner_schema = _type_hint_to_schema(
                _schema_type_hint, inner_annotation=list_inner_ann
            )

    # Apply constraint metadata to the INNER schema first, then decide
    # whether to wrap it in an anyOf for Optional[...] params. FA
    # coerces numeric ``ge``/``gt``/``le``/``lt`` values to the param
    # type (``Path(gt=3)`` on a ``float`` param → ``3.0``). Mirror that
    # so the emitted schema matches byte-for-byte.
    _numeric_keys = {"ge", "gt", "le", "lt", "multiple_of"}
    _is_float_type = inner_schema.get("type") == "number"
    for ck, sk in (
        ("ge", "minimum"),
        ("gt", "exclusiveMinimum"),
        ("le", "maximum"),
        ("lt", "exclusiveMaximum"),
        ("min_length", "minLength"),
        ("max_length", "maxLength"),
        ("pattern", "pattern"),
        ("regex", "pattern"),
        ("multiple_of", "multipleOf"),
    ):
        v = param.get(ck)
        if v is not None:
            if _is_float_type and ck in _numeric_keys and isinstance(v, int) and not isinstance(v, bool):
                v = float(v)
            inner_schema[sk] = v

    # ``set[T]`` / ``frozenset[T]`` query params emit ``uniqueItems: True``
    # — Pydantic adds it natively for BaseModel fields; we mirror it here
    # for standalone Query/Header/Cookie/Path parameters.
    if _is_unique_container and isinstance(inner_schema, dict):
        if inner_schema.get("type") == "array":
            inner_schema["uniqueItems"] = True

    # Enum params: when the enum class has been hoisted into
    # `components.schemas`, emit `$ref` — FA does this. For `Literal`
    # (which has no class) or pending pre-pass, inline the `{enum,
    # type, title}` form.
    if param.get("_enum_values") is not None:
        enum_cls = param.get("enum_class")
        if enum_cls is not None and isinstance(enum_cls, type):
            inner_schema = {"$ref": f"#/components/schemas/{enum_cls.__name__}"}
        else:
            _inner_type = inner_schema.get("type", "string")
            inner_schema = {
                "enum": list(param["_enum_values"]),
                "type": _inner_type,
                "title": p_name,
            }

    # Optional[...] params → `anyOf: [<inner>, {type: null}]`
    if param.get("_is_optional"):
        schema = {"anyOf": [inner_schema, {"type": "null"}]}
    else:
        schema = inner_schema

    p: dict[str, Any] = {
        "name": p_name,
        "in": param["kind"],
        "required": _required_for_schema,
        "schema": schema,
    }
    # Only emit `schema.default` for plain scalar defaults. Pydantic
    # `default_factory=list` / `default_factory=dict` produce runtime
    # values that FastAPI doesn't surface in OpenAPI (to mirror what FA
    # does, we skip any empty collection default).
    _dv = param.get("default_value")
    # Parameter-model synthetic field params store a sentinel "missing"
    # placeholder as default_value — the real default lives on the
    # Pydantic FieldInfo. Pull it from there for the schema.
    if param.get("_param_model_field_info") is not None:
        from fastapi_rs._introspect import _PARAM_MODEL_MISSING as _PMM
        from pydantic_core import PydanticUndefined as _PU
        if _dv is _PMM:
            _field_info = param["_param_model_field_info"]
            raw_default = getattr(_field_info, "default", _PU)
            df = getattr(_field_info, "default_factory", None)
            if df is not None:
                try:
                    _dv = df()
                except Exception:  # noqa: BLE001
                    _dv = None
            elif raw_default is _PU:
                _dv = None
            else:
                _dv = raw_default
    if _dv is not None:
        # Avoid putting non-JSON-serializable sentinels in the schema.
        try:
            import json as _json
            _json.dumps(_dv, default=str)
            p["schema"]["default"] = _dv
        except Exception:  # noqa: BLE001
            pass
    # Title: user-provided (via Query(title=...)) wins. For Enum params
    # FastAPI surfaces the enum class name (e.g. `Color`). For Header
    # params the on-the-wire name is the alias (`X-Request-Id`), which FA
    # uses verbatim as the title. Otherwise auto-derive from the
    # parameter name (`offset` → `"Offset"`).
    title = param.get("title")
    if not title:
        enum_cls = param.get("enum_class")
        if enum_cls is not None and isinstance(enum_cls, type):
            title = enum_cls.__name__
    if not title and param.get("kind") == "header" and param.get("alias"):
        # FA preserves hyphens in header aliases when the user declared
        # the header directly (``Header(alias="user-agent")`` →
        # ``"User-Agent"``). For **param-model fields**, title comes
        # from the Pydantic ``validation_alias`` (if set) or the raw
        # field name — either way split on ``_`` into space-separated
        # words (``x_tag`` → ``"X Tag"``, ``p_val_alias`` → ``"P Val Alias"``).
        pm_field = param.get("_param_model_field_name")
        if pm_field:
            _pm_fi = param.get("_param_model_field_info")
            _val_alias = None
            if _pm_fi is not None:
                _va = getattr(_pm_fi, "validation_alias", None)
                if isinstance(_va, str):
                    _val_alias = _va
            title_src = _val_alias or pm_field
            title = " ".join(
                w.capitalize() for w in title_src.split("_") if w
            )
        else:
            raw_alias = param["alias"]
            if "-" in raw_alias:
                title = "-".join(w.capitalize() for w in raw_alias.split("-") if w)
            else:
                title = " ".join(
                    w.capitalize() for w in raw_alias.split("_") if w
                )
    if not title:
        # Title derivation priority (matches FA):
        #  1. Parameter-model synthetic field → the emitted param
        #     name (already resolved above: alias if set, else field).
        #  2. Alias (``Query(alias="p_alias")`` → ``"P Alias"``).
        #  3. Param Python variable name.
        # Header titles were already computed above from the alias
        # (hyphen-preserving), so only cookies / queries hit this path.
        if param.get("_param_model_field_name"):
            title_source = p_name
        else:
            title_source = param.get("alias") or param["name"]
        # FA's rule: title splits on ``_`` into space-separated words but
        # PRESERVES hyphens (``item-query`` → ``"Item-Query"``, not
        # ``"Item Query"``). Matches Pydantic v2 model_json_schema
        # behaviour for aliased fields.
        if "-" in title_source:
            title = "-".join(
                " ".join(w.capitalize() for w in seg.split("_") if w) or seg
                for seg in title_source.split("-")
            )
        else:
            title = " ".join(
                w.capitalize()
                for w in title_source.split("_")
                if w
            )
    # Don't overwrite a title on a pure ``$ref`` schema (FA leaves the
    # referenced component's own title in place). Also skip ``anyOf``
    # wrappers whose inner is a $ref — FA emits the $ref without title.
    def _is_pure_ref(sch):
        return isinstance(sch, dict) and set(sch.keys()) == {"$ref"}
    _top = p["schema"]
    if not (_is_pure_ref(_top)):
        _top["title"] = title
    # Description appears on BOTH the parameter and its inner schema in
    # FastAPI-generated OpenAPI. Keeping them in sync is important for
    # docs frontends (Swagger/Redoc) that render them from either side.
    if param.get("description"):
        p["description"] = param["description"]
        if not _is_pure_ref(_top):
            _top["description"] = param["description"]
    if param.get("deprecated"):
        p["deprecated"] = True
        # Pydantic 2.10+ also surfaces ``deprecated`` INSIDE the schema
        # (not just at the parameter level) — FastAPI tests assert on
        # both locations. Emit it on the inner schema unless we're
        # sitting on a pure $ref (which can't carry extra keys).
        if not _is_pure_ref(_top):
            _top["deprecated"] = True
    # OpenAPI: ``Query(example="Alice")`` → ``parameter.example``.
    # ``Query(examples=[...])`` (list) → ``schema.examples`` (FA's
    # OpenAPI 3.1 inline form). ``Query(examples={"n1": {"value": ...}})``
    # (named dict) → ``parameter.examples`` (OpenAPI 3.0+ spec).
    # ``openapi_examples=`` is always the named-dict form at parameter
    # level.
    if param.get("example") is not None:
        p["example"] = param["example"]
    if param.get("examples") is not None:
        raw = param["examples"]
        if isinstance(raw, dict):
            p["examples"] = raw
        elif isinstance(raw, (list, tuple)):
            p["schema"]["examples"] = list(raw)
        else:
            p["schema"]["examples"] = [raw]
    if param.get("openapi_examples") is not None:
        p["examples"] = param["openapi_examples"]
    return p


def _hoist_body_schema(operation: dict[str, Any], components_schemas: dict[str, Any]) -> None:
    """Pull inline `Body_<handler>` schemas out of `requestBody.content` and
    register them in `components.schemas`, rewriting the inline schema to a
    `$ref`. This matches FastAPI's generated openapi.json, which always
    references a named component instead of inlining.

    FA actually names the body ``Body_<operation_id>`` (where operation_id
    is whatever ``generate_unique_id_function`` returned — e.g.
    ``foo_post_root``). Our introspection-time name uses the endpoint
    function name because the operationId cascade isn't resolved yet.
    Rename here so custom ``generate_unique_id_function`` flows through.
    """
    rb = operation.get("requestBody")
    if not isinstance(rb, dict):
        return
    op_id = operation.get("operationId")
    content = rb.get("content") or {}
    for media_type, media_obj in content.items():
        sch = (media_obj or {}).get("schema")
        if not isinstance(sch, dict):
            continue
        # Case 1: schema is ``$ref`` to an existing Body_* component.
        # Rename the component to ``Body_<operation_id>`` and rewrite
        # the $ref to match.
        ref = sch.get("$ref")
        if isinstance(ref, str) and "/Body_" in ref:
            old_name = ref.split("/")[-1]
            if old_name in components_schemas:
                target_name = old_name
                if isinstance(op_id, str) and op_id:
                    target_name = f"Body_{op_id}"
                if target_name != old_name:
                    body_schema = components_schemas.pop(old_name)
                    if isinstance(body_schema, dict) and body_schema.get("title") == old_name:
                        body_schema["title"] = target_name
                    components_schemas[target_name] = body_schema
                    media_obj["schema"] = {"$ref": f"#/components/schemas/{target_name}"}
            continue
        # Case 2: inline ``Body_<handler>`` schema — promote to
        # components/schemas with the operationId-derived name.
        title = sch.get("title")
        if not (isinstance(title, str) and title.startswith("Body_")):
            continue
        target_title = title
        if isinstance(op_id, str) and op_id:
            target_title = f"Body_{op_id}"
            if target_title != title:
                sch["title"] = target_title
        components_schemas.setdefault(target_title, sch)
        media_obj["schema"] = {"$ref": f"#/components/schemas/{target_title}"}


def _build_form_file_body(
    route: dict[str, Any],
    form_params: list[dict[str, Any]],
    file_params: list[dict[str, Any]],
    ordered_mixed: list[tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Synthesize a `Body_<handler>` request body for Form/File endpoints
    by building a Pydantic model at schema-generation time. Going through
    Pydantic gives us `anyOf: [<T>, {type: null}]` for Optional fields,
    `format: "password"` (via SecretStr), exact `pattern` / `minLength` /
    `maxLength` emission, and matches FA's generated schema byte-for-byte.
    """
    from pydantic import create_model, Field as _PField
    from typing import Optional, Any as _Any

    # Form taking a BaseModel directly (``data: FormData = Form()``) —
    # FA emits the MODEL's own schema under
    # ``components.schemas.<ModelName>`` rather than synthesising a
    # ``Body_<endpoint>`` wrapper. Our ``_maybe_expand_param_models``
    # already flattened the model into individual form extraction
    # entries; detect this case by checking every ``form_params`` entry
    # shares the same ``_param_model_owner`` and class.
    if form_params and not file_params:
        from pydantic import BaseModel as _BM
        _owners = {fp.get("_param_model_owner") for fp in form_params}
        _classes = {fp.get("_param_model_class") for fp in form_params}
        _model_cls = next(iter(_classes), None) if len(_classes) == 1 else None
        if (
            len(_owners) == 1
            and next(iter(_owners)) is not None
            and _model_cls is not None
            and isinstance(_model_cls, type)
            and issubclass(_model_cls, _BM)
        ):
            _marker = form_params[0].get("_raw_marker")
            _media = getattr(_marker, "media_type", None) or "application/x-www-form-urlencoded"
            # FormData's schema lands in components.schemas via
            # ``_collect_schemas`` (which walks ``_param_model_class``).
            # FA adds ``additionalProperties: False`` for form models to
            # reject unknown fields; we patch that in post-pass when the
            # form is the sole body.
            return {
                "content": {
                    _media: {
                        "schema": {
                            "$ref": f"#/components/schemas/{_model_cls.__name__}"
                        }
                    }
                },
                "required": True,
            }

    type_map = {"int": int, "float": float, "bool": bool, "str": str, "bytes": bytes}
    field_defs: dict[str, Any] = {}

    def _to_py_type(th: str) -> Any:
        if th.startswith("list_"):
            inner = th[5:]
            return list[type_map.get(inner, str)]
        return type_map.get(th, str)

    import copy as _copy
    for fp in form_params:
        # For parameter-model synthetic fields (``pm_<owner>__<field>``)
        # use the ORIGINAL field name so the emitted schema uses the
        # user's model's field names, not our internal synthesized ones.
        # Also for param-model fields, the synthetic extraction entry
        # is always ``required=False`` (so we can collect missing-value
        # errors uniformly) — but for SCHEMA emission we must defer
        # to the underlying model's ``FieldInfo.is_required()``.
        pm_field_info = fp.get("_param_model_field_info")
        if pm_field_info is not None:
            name = fp.get("_param_model_field_name") or fp["name"]
            ann = fp.get("_param_model_field_ann") or pm_field_info.annotation or str
            field_defs[name] = (ann, pm_field_info)
            continue
        name = fp["name"]
        raw_marker = fp.get("_raw_marker")
        raw_ann = fp.get("_raw_annotation")
        if raw_marker is not None:
            # Use the UNWRAPPED annotation (strip Annotated so Pydantic
            # doesn't double-merge the FieldInfo inside Annotated with
            # our explicit default). Clone the marker and set `default`
            # from the signature so required-ness matches FastAPI.
            import typing as _typing
            if raw_ann is not None and _typing.get_origin(raw_ann) is _typing.Annotated:
                use_ann = _typing.get_args(raw_ann)[0]
            else:
                use_ann = raw_ann if raw_ann is not None else str
            m = _copy.copy(raw_marker)
            if fp.get("has_default") and not fp.get("required", True):
                try:
                    m.default = fp.get("default_value")
                    if getattr(m, "default_factory", None) is not None:
                        m.default_factory = None
                except Exception:  # noqa: BLE001
                    pass
            field_defs[name] = (use_ann, m)
            continue
        py_type = _to_py_type(fp.get("type_hint", "str"))
        if fp.get("_is_optional"):
            py_type = Optional[py_type]
        kwargs = {}
        for ck in ("gt", "ge", "lt", "le", "min_length", "max_length",
                   "pattern", "regex", "multiple_of"):
            v = fp.get(ck)
            if v is not None:
                kwargs["pattern" if ck == "regex" else ck] = v
        if fp.get("description"):
            kwargs["description"] = fp["description"]
        if fp.get("title"):
            kwargs["title"] = fp["title"]
        if fp.get("required", True):
            default_sentinel = ...
        else:
            default_sentinel = fp.get("default_value")
        field_defs[name] = (py_type, _PField(default_sentinel, **kwargs))

    body_title = f"Body_{route.get('handler_name', 'endpoint')}"
    BodyModel = create_model(body_title, **field_defs) if field_defs else None
    try:
        # ``by_alias=True`` makes Pydantic emit ``Form(alias="p_alias")``
        # as ``properties.p_alias`` (FA's shape). Without this the
        # schema uses the Python parameter name and diverges.
        body_schema = (
            BodyModel.model_json_schema(mode="validation", by_alias=True)
            if BodyModel is not None
            else {"properties": {}, "type": "object", "title": body_title}
        )
    except Exception:  # noqa: BLE001
        body_schema = {"properties": {}, "type": "object", "title": body_title}
    # Strip nested $defs — the outer hoisting pass will move them.
    body_schema.pop("$defs", None)

    # File params: FastAPI emits JSON-Schema `contentMediaType:
    # application/octet-stream` (no `format: binary`) for UploadFile
    # fields, and wraps `list[UploadFile]` as `type: array, items:
    # {type: string, contentMediaType: ...}`. Our Pydantic-based builder
    # doesn't know about UploadFile semantics, so we splice them in
    # directly after the fact.
    body_schema.setdefault("properties", {})
    body_schema.setdefault("required", [])
    for fp in file_params:
        # ``File(alias="p_alias")`` → emit the on-the-wire ``p_alias``
        # as the property name (and ``"P Alias"`` as the title), not
        # the Python param name. FA does the same.
        field_name = fp.get("_param_model_field_name")
        if field_name is not None:
            name = field_name
        elif fp.get("alias"):
            name = fp["alias"]
        else:
            name = fp["name"]
        base_item = {
            "type": "string",
            "contentMediaType": "application/octet-stream",
        }
        # Detect list-of-files via type_hint OR the unwrapped annotation
        # (``list[UploadFile]`` has type_hint="file" but is still a list).
        import typing as _fp_typing
        _ua = fp.get("_unwrapped_annotation")
        _is_list = fp.get("type_hint", "").startswith("list_") or (
            _fp_typing.get_origin(_ua) in (list, tuple, set, frozenset)
            if _ua is not None
            else False
        )
        if _is_list:
            schema = {"items": base_item, "type": "array"}
        else:
            schema = dict(base_item)
        # Optional File / UploadFile → FA emits ``anyOf: [<base>, {type: null}]``.
        if fp.get("_is_optional"):
            schema = {"anyOf": [schema, {"type": "null"}]}
        title_words = " ".join(w.capitalize() for w in name.split("_") if w)
        schema["title"] = title_words
        if fp.get("description"):
            schema["description"] = fp["description"]
        body_schema["properties"][name] = schema
        if fp.get("required", True) and name not in body_schema["required"]:
            body_schema["required"].append(name)
    if not body_schema["required"]:
        body_schema.pop("required", None)

    # Reorder properties AND required to match the handler's signature
    # declaration order (FastAPI preserves that — our Pydantic-generated
    # body puts form fields first then file fields, which differs when
    # the user wrote `title, description, file, tags`).
    if ordered_mixed:
        props = body_schema.get("properties", {})
        ordered_keys: list[str] = []
        for _, p in ordered_mixed:
            if p["name"] in props and p["name"] not in ordered_keys:
                ordered_keys.append(p["name"])
        # Append any keys that weren't in ordered_mixed (defensive).
        for k in props:
            if k not in ordered_keys:
                ordered_keys.append(k)
        body_schema["properties"] = {k: props[k] for k in ordered_keys}
        # Reorder required to match — FA emits them in signature order.
        if body_schema.get("required"):
            existing = set(body_schema["required"])
            body_schema["required"] = [k for k in ordered_keys if k in existing]

    media_type = "multipart/form-data" if file_params else "application/x-www-form-urlencoded"
    # FA only emits ``required: True`` when the body has at least one
    # actually-required field. An endpoint like
    # ``file: bytes | None = File(default=None)`` has
    # ``required: False`` → no ``required`` key in requestBody.
    _has_required = any(
        fp.get("required", True) for fp in (form_params + file_params)
    )
    body: dict[str, Any] = {
        "content": {media_type: {"schema": body_schema}},
    }
    if _has_required:
        body["required"] = True
    return body


def _build_request_body(param: dict[str, Any]) -> dict[str, Any]:
    """Build an OpenAPI requestBody from a body parameter."""
    model_class = param.get("model_class")
    # ``Model | None`` / ``Optional[Model]`` body params surface as a
    # ``_TypeAdapterProxy`` wrapping the Union; FA emits
    # ``anyOf: [{$ref: Model}, {type: null}]`` for them. Unwrap the
    # Union so we can hand each arm through ``_model_ref`` / a plain
    # type fragment.
    annotation = getattr(model_class, "_annotation", None)
    import typing as _typing
    # Container body params — ``list[Item]``, ``dict[str, Item]``, etc.
    # Pydantic's TypeAdapter emits the correct JSON schema (array/obj
    # with ``$ref`` items); use it directly and normalise ``$defs``.
    _origin = _typing.get_origin(annotation) if annotation is not None else None
    if _origin in (list, tuple, set, frozenset, dict):
        try:
            from pydantic import TypeAdapter as _TA
            container_schema = _TA(annotation).json_schema(mode="validation")
            # Drop nested $defs — the outer
            # ``_collect_model_schemas`` pass surfaces those to
            # components.schemas and we want internal refs rewritten.
            container_schema.pop("$defs", None)
            container_schema = _rewrite_defs_refs(container_schema)
            # FA titles the body ``Images`` (matching the handler param
            # name capitalized); mirror that if Pydantic didn't already.
            if "title" not in container_schema and param.get("name"):
                container_schema["title"] = param["name"].replace("_", " ").title()
            media_type = param.get("media_type") or "application/json"
            content: dict[str, Any] = {media_type: {"schema": container_schema}}
            body: dict[str, Any] = {"content": content}
            if param.get("required", True):
                body["required"] = True
            if param.get("description"):
                body["description"] = param["description"]
            return body
        except Exception:  # noqa: BLE001
            pass
    # Handle both ``typing.Union`` and PEP 604 (``X | Y``) — in 3.10+ the
    # latter resolves to ``types.UnionType``. ``get_origin`` returns
    # ``Union`` for the former and ``UnionType`` for the latter.
    import types as _types
    _u_origin = _typing.get_origin(annotation) if annotation is not None else None
    if annotation is not None and _u_origin in (_typing.Union, _types.UnionType):
        arms = _typing.get_args(annotation)
        non_none = [a for a in arms if a is not type(None)]
        has_none = any(a is type(None) for a in arms)
        any_of: list[Any] = []
        title_from: Any = None
        for arm in non_none:
            arm_ref = _model_ref(arm, mode="validation")
            if arm_ref is not None:
                any_of.append(arm_ref)
                if title_from is None:
                    title_from = arm
            else:
                frag = _type_hint_to_schema(_get_type_name(arm))
                if frag and frag != {"type": "object"}:
                    any_of.append(frag)
                else:
                    any_of.append({"type": "object"})
        if has_none:
            any_of.append({"type": "null"})
        if any_of:
            body_schema: dict[str, Any] = {"anyOf": any_of}
            # FA parity: title is the handler param name capitalized
            # (``item`` → ``Item``), not the first arm's class name.
            pname = param.get("name")
            if pname:
                body_schema["title"] = (
                    pname.replace("_", " ").title().replace(" ", "")
                )
            elif title_from is not None:
                body_schema["title"] = getattr(title_from, "__name__", "Body")
            media_type = param.get("media_type") or "application/json"
            content: dict[str, Any] = {media_type: {"schema": body_schema}}
            body: dict[str, Any] = {"content": content}
            if param.get("required", True):
                body["required"] = True
            if param.get("description"):
                body["description"] = param["description"]
            return body
    ref = _model_ref(model_class, mode="validation") if model_class is not None else None
    if ref is None and model_class is not None:
        # ``_TypeAdapterProxy`` wraps non-BaseModel annotations like
        # dataclasses / Unions. Unwrap so we can still $ref when the
        # inner annotation is a dataclass.
        inner = getattr(model_class, "_annotation", None)
        if inner is not None and _is_dataclass_type(inner):
            ref = {"$ref": f"#/components/schemas/{inner.__name__}"}
    if ref is not None:
        body_schema = ref
    elif model_class is not None and hasattr(model_class, "model_json_schema"):
        body_schema = model_class.model_json_schema(mode="validation")
    elif model_class is not None and _is_dataclass_type(model_class):
        # Python ``@dataclass`` bodies — FA uses a Pydantic TypeAdapter
        # to produce the JSON schema and registers the dataclass under
        # ``components.schemas`` with a ``$ref`` back to it.
        body_schema = {"$ref": f"#/components/schemas/{model_class.__name__}"}
    else:
        body_schema = {"type": "object"}

    # media_type override (e.g., application/xml, application/octet-stream)
    media_type = param.get("media_type") or "application/json"

    content: dict[str, Any] = {media_type: {"schema": body_schema}}
    if param.get("example") is not None:
        content[media_type]["example"] = param["example"]
    # ``Body(examples=[...])`` — FA inlines as a LIST on the inner
    # schema (OpenAPI 3.1 allows ``schema.examples`` as an array).
    # ``Body(openapi_examples={"name": {"value": ...}})`` — FA emits at
    # content level as a named-examples DICT.
    if param.get("examples") is not None:
        raw = param["examples"]
        if isinstance(raw, list):
            # Inline as schema.examples (the new Pydantic-ish form).
            # Wrap $ref in an allOf to permit extra keys.
            inner = body_schema
            if isinstance(inner, dict) and "$ref" in inner:
                inner.setdefault("examples", list(raw))
            else:
                inner.setdefault("examples", list(raw))
        elif isinstance(raw, dict):
            content[media_type]["examples"] = raw
    if param.get("openapi_examples") is not None:
        content[media_type]["examples"] = _normalize_examples(param["openapi_examples"])

    # Match FA's ``get_openapi_operation_request_body``: emit
    # ``"required"`` only when the body is required, not always.
    body: dict[str, Any] = {"content": content}
    if param.get("required", True):
        body["required"] = True
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


def _rewrite_defs_refs(obj: Any) -> Any:
    """Recursively rewrite any ``#/$defs/Foo`` reference (as either a
    `$ref` key or a bare string value like `discriminator.mapping`
    entries) into ``#/components/schemas/Foo``. Also drops Pydantic-
    emitted ``default: null`` entries that FastAPI strips in its own
    post-processing pass AND normalises generic class names
    (`Page[Item]` → `Page_Item_`) in the rewritten ref targets.
    """

    def _rewrite_ref(v: str) -> str:
        if v.startswith("#/$defs/"):
            tail = v[len("#/$defs/"):]
            return "#/components/schemas/" + _normalize_model_name(tail)
        if v.startswith("#/components/schemas/"):
            tail = v[len("#/components/schemas/"):]
            # Handle optional `-Input` / `-Output` suffix on split models
            for suffix in ("-Input", "-Output"):
                if tail.endswith(suffix):
                    base = tail[: -len(suffix)]
                    return "#/components/schemas/" + _normalize_model_name(base) + suffix
            return "#/components/schemas/" + _normalize_model_name(tail)
        return v

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            # Strip ``default: null`` on Optional fields — FA omits it
            # from the served schema. Preserve ``default: []`` /
            # ``default: {}`` etc. (FA's serialization_defaults_required
            # pydantic mode keeps those).
            if k == "default" and v is None:
                continue
            if isinstance(v, str) and (v.startswith("#/$defs/") or v.startswith("#/components/schemas/")):
                out[k] = _rewrite_ref(v)
            else:
                out[k] = _rewrite_defs_refs(v)
        return out
    if isinstance(obj, list):
        return [_rewrite_defs_refs(item) for item in obj]
    if isinstance(obj, str) and (obj.startswith("#/$defs/") or obj.startswith("#/components/schemas/")):
        return _rewrite_ref(obj)
    return obj


def _json_encode_fallback(obj) -> str:
    import json
    try:
        return json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        return ""


# Usage tracker: id(model_class) → set of {"input", "output"}. Populated
# during operation building so _collect_schemas knows whether to emit
# `<Name>-Input` / `<Name>-Output` split variants for dual-use models.
_MODEL_USAGE: dict[int, set[str]] = {}
# Whether the current schema build allows ``-Input``/``-Output`` splits.
# ``FastAPI(separate_input_output_schemas=False)`` sets this to False and
# `_model_ref` / post-pass A then emit a single merged schema per model.
_SEPARATE_INPUT_OUTPUT: bool = True


def _normalize_model_name(name: str) -> str:
    """FastAPI rewrites generic parameter class names from Pydantic's
    `Name[T]` form into `Name_T_` so they're valid JSON Schema component
    names (which must match `^[a-zA-Z0-9.\\-_]+$`). Apply the same
    transformation to match the generated openapi.json byte-for-byte.
    """
    if not isinstance(name, str):
        return name
    # `Page[Item]` → `Page_Item_`; `Box[str, int]` → `Box_str_int_`; etc.
    return name.replace("[", "_").replace("]", "_").replace(", ", "_").replace(",", "_").replace(" ", "")


def _note_model_usage(model_class, ctx: str) -> None:
    """Record that `model_class` is referenced in `ctx` ("input" or
    "output"), then recurse into its fields so nested models inherit the
    same usage. This mirrors FastAPI's logic: a model used as an output
    root propagates the "output" usage through every nested model it
    references, so deep `List[Node]` style graphs split correctly.
    """
    if model_class is None or not hasattr(model_class, "model_json_schema"):
        return
    existing = _MODEL_USAGE.setdefault(id(model_class), set())
    if ctx in existing:
        return
    existing.add(ctx)
    # Walk nested model references.
    fields = getattr(model_class, "model_fields", None) or {}
    import typing as _typing
    try:
        from pydantic import BaseModel as _BM
    except ImportError:  # pragma: no cover
        return
    for finfo in fields.values():
        ann = getattr(finfo, "annotation", None)
        _walk_annotation_for_usage(ann, ctx, _typing, _BM)


def _walk_annotation_for_usage(ann, ctx, _typing, _BM) -> None:
    if ann is None:
        return
    origin = _typing.get_origin(ann)
    if origin is not None:
        for sub in _typing.get_args(ann):
            _walk_annotation_for_usage(sub, ctx, _typing, _BM)
        return
    try:
        if isinstance(ann, type) and issubclass(ann, _BM):
            _note_model_usage(ann, ctx)
    except Exception:  # noqa: BLE001
        pass


def _flatten_annotation_types(ann):
    """Yield every concrete type that appears in a (possibly generic)
    annotation — used to seed the schema-registration pass."""
    import typing as _typing
    if ann is None:
        return
    origin = _typing.get_origin(ann)
    if origin is not None:
        for sub in _typing.get_args(ann):
            yield from _flatten_annotation_types(sub)
        return
    if isinstance(ann, type):
        yield ann


def _model_ref(model_class, mode: str | None = None) -> dict[str, str] | None:
    """Return a ``{"$ref": "#/components/schemas/Name"}`` dict for a Pydantic model.

    FastAPI splits a model into `<Name>-Input` + `<Name>-Output` when
    (1) it is referenced from BOTH an input (request body) and an output
    (response) context, AND (2) the model is self-recursive (has a
    field referring back to itself, directly or transitively). Other
    models keep the plain `<Name>` ref.
    """
    name = getattr(model_class, "__name__", None)
    if not name:
        return None
    if not hasattr(model_class, "model_json_schema"):
        # Python dataclass — still emit a $ref to the shared component
        # (we register its schema via _collect_model_schemas).
        if _is_dataclass_type(model_class):
            return {"$ref": f"#/components/schemas/{name}"}
        return None
    name = _normalize_model_name(name)
    usage = _MODEL_USAGE.get(id(model_class), set())
    _has_cf = _model_has_computed_fields(model_class)
    # FA forces split whenever the model has a ``computed_field`` (even
    # with ``separate_input_output_schemas=False``), because the val/ser
    # shapes genuinely differ. Otherwise honor the flag.
    _allow_split = _SEPARATE_INPUT_OUTPUT or _has_cf
    split = (
        _allow_split
        and "input" in usage and "output" in usage
        and (
            _model_is_self_recursive(model_class)
            or _val_ser_schemas_differ(model_class)
        )
    )
    if split and mode == "serialization":
        return {"$ref": f"#/components/schemas/{name}-Output"}
    if split and mode == "validation":
        return {"$ref": f"#/components/schemas/{name}-Input"}
    return {"$ref": f"#/components/schemas/{name}"}


def _val_ser_schemas_differ(model_class) -> bool:
    """True if the model's validation and serialization JSON schemas
    produce different shapes (e.g. ``json_schema_serialization_defaults_required``
    or ``computed_field`` on a property).
    """
    try:
        val = model_class.model_json_schema(mode="validation")
        ser = model_class.model_json_schema(mode="serialization")
        return val != ser
    except Exception:  # noqa: BLE001
        return False


def _model_has_computed_fields(model_class) -> bool:
    """True if the model has any Pydantic ``@computed_field``. FA forces
    split emission for these regardless of ``separate_input_output_schemas``.
    """
    try:
        cf = getattr(model_class, "model_computed_fields", None)
        return bool(cf)
    except Exception:  # noqa: BLE001
        return False


def _model_is_self_recursive(model_class) -> bool:
    """True if the model contains a field whose annotation references the
    same class, directly or transitively via nested containers / Unions.
    """
    try:
        fields = getattr(model_class, "model_fields", None) or {}
    except Exception:  # noqa: BLE001
        return False
    import typing as _typing

    def _refs(ann, seen):
        if ann is None:
            return False
        origin = _typing.get_origin(ann)
        if origin is not None:
            return any(_refs(a, seen) for a in _typing.get_args(ann))
        try:
            if isinstance(ann, type) and ann is model_class:
                return True
            if isinstance(ann, type) and hasattr(ann, "model_fields") and ann not in seen:
                seen.add(ann)
                for f in (getattr(ann, "model_fields", None) or {}).values():
                    if _refs(getattr(f, "annotation", None), seen):
                        return True
        except Exception:  # noqa: BLE001
            return False
        return False

    for finfo in fields.values():
        if _refs(getattr(finfo, "annotation", None), {model_class}):
            return True
    return False


def _collect_schemas(
    route: dict[str, Any], schemas: dict[str, Any]
) -> None:
    """Extract Pydantic model ``$defs`` into the shared components/schemas bucket."""
    # From route params (body models). ``_TypeAdapterProxy`` wraps
    # non-BaseModel annotations (``list[Item]``, ``Foo | None``); walk
    # the inner annotation too so inner ``BaseModel`` arms surface in
    # components.schemas.
    for param in route.get("params", []):
        model_class = param.get("model_class")
        _collect_model_schemas(model_class, schemas)
        inner = getattr(model_class, "_annotation", None)
        if inner is not None:
            for sub in _flatten_annotation_types(inner):
                _collect_model_schemas(sub, schemas)
        # Param-model expansion's owning class. For ``Form()`` models we
        # emit the BaseModel schema in ``components.schemas`` (so
        # ``$ref: FormData`` resolves); for ``Query()`` / ``Header()`` /
        # ``Cookie()`` param models FA does NOT emit a component — the
        # model's fields are flattened into individual ``parameters``.
        _pmc = param.get("_param_model_class")
        if _pmc is not None and param.get("kind") in ("form", "file"):
            _collect_model_schemas(_pmc, schemas)

    # From route.responses (extra status codes with models).
    # ``responses={404: {"model": list[Message]}}`` — walk container
    # annotations too so the inner ``Message`` model lands in
    # components.schemas.
    for resp_info in (route.get("responses") or {}).values():
        if isinstance(resp_info, dict):
            resp_model = resp_info.get("model")
            _collect_model_schemas(resp_model, schemas)
            for sub in _flatten_annotation_types(resp_model):
                _collect_model_schemas(sub, schemas)

    # response_model — same container-walk. SSE routes use the
    # AsyncIterable[...] form as response_model; ServerSentEvent itself
    # is a transport wrapper that FA excludes from components.schemas.
    _is_sse_route = False
    try:
        from fastapi_rs.responses import EventSourceResponse as _ESR_cs
        _rc_cs = route.get("response_class")
        if _rc_cs is not None and isinstance(_rc_cs, type) and issubclass(_rc_cs, _ESR_cs):
            _is_sse_route = True
    except Exception:  # noqa: BLE001
        pass
    resp_m = route.get("response_model")
    if not _is_sse_route:
        _collect_model_schemas(resp_m, schemas)
    for sub in _flatten_annotation_types(resp_m):
        if _is_sse_route:
            try:
                from fastapi_rs.sse import ServerSentEvent as _SSE_CS
                if isinstance(sub, type) and issubclass(sub, _SSE_CS):
                    continue
            except Exception:  # noqa: BLE001
                pass
        _collect_model_schemas(sub, schemas)

    # Callback routes' response models
    for cb_model in _walk_callback_models(route.get("callbacks")):
        _collect_model_schemas(cb_model, schemas)
        for sub in _flatten_annotation_types(cb_model):
            _collect_model_schemas(sub, schemas)


def _is_dataclass_type(obj: Any) -> bool:
    """Return True for a plain ``@dataclass`` class (not a Pydantic model)."""
    try:
        import dataclasses as _dc

        return (
            isinstance(obj, type)
            and _dc.is_dataclass(obj)
            and not hasattr(obj, "model_json_schema")
        )
    except Exception:  # noqa: BLE001
        return False


def _collect_model_schemas(model_class, schemas: dict[str, Any]) -> None:
    """Extract Pydantic model schema and its $defs into the shared components/schemas bucket.

    Registers the model itself under its class name so operations can
    reference it via ``$ref: #/components/schemas/ModelName``.  Also
    promotes any ``$defs`` (nested models) into the same flat bucket.
    """
    if model_class is None:
        return
    # Python dataclass support — FA runs it through a Pydantic TypeAdapter
    # which produces a regular JSON schema; surface it under the class
    # name so body $refs resolve.
    if _is_dataclass_type(model_class):
        try:
            from pydantic import TypeAdapter as _TA
            schema = _TA(model_class).json_schema(mode="validation")
            name = model_class.__name__
            # Hoist $defs so nested models share the components bucket.
            if isinstance(schema, dict) and "$defs" in schema:
                for dname, dschema in schema["$defs"].items():
                    if dname not in schemas:
                        schemas[dname] = dschema
                schema = {k: v for k, v in schema.items() if k != "$defs"}
            schema = _rewrite_defs_refs(schema)
            if name not in schemas:
                schemas[name] = schema
        except Exception:  # noqa: BLE001
            pass
        return
    if not hasattr(model_class, "model_json_schema"):
        return
    # Pydantic v2 splits a model into two JSON schemas whenever its
    # validation and serialization shapes differ (`computed_field`,
    # recursive forward refs, validation-only aliases, etc.) AND FastAPI
    # emits both when the model is used in both input and output contexts
    # anywhere in the app. We record both variants proactively — the
    # `_used_as` hint at schema-resolution time picks the right ref.
    try:
        val_schema = model_class.model_json_schema(mode="validation")
    except Exception:
        val_schema = None
    try:
        ser_schema = model_class.model_json_schema(mode="serialization")
    except Exception:
        ser_schema = None

    # FA 0.110+: models with ``model_config={"val_json_bytes": "base64"}``
    # (or ``ser_json_bytes``) emit ``contentEncoding: base64`` +
    # ``contentMediaType: application/octet-stream`` on byte fields
    # instead of the plain ``format: binary`` Pydantic defaults to.
    # Apply the post-pass here.
    try:
        cfg = getattr(model_class, "model_config", None) or {}
        val_b64 = cfg.get("val_json_bytes") == "base64"
        ser_b64 = cfg.get("ser_json_bytes") == "base64"
    except Exception:  # noqa: BLE001
        val_b64 = False
        ser_b64 = False

    def _apply_base64_to_bytes(schema: dict[str, Any] | None) -> None:
        if not isinstance(schema, dict):
            return
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else None
        if props:
            for _, field_schema in props.items():
                # Pydantic emits ``format: binary`` for single-mode
                # ``val_json_bytes``/``ser_json_bytes`` and
                # ``format: base64url`` when BOTH are set. Both cases
                # should translate to FA's ``contentEncoding: base64`` +
                # ``contentMediaType: application/octet-stream``.
                if (
                    isinstance(field_schema, dict)
                    and field_schema.get("type") == "string"
                    and field_schema.get("format") in ("binary", "base64url")
                ):
                    field_schema.pop("format", None)
                    field_schema["contentEncoding"] = "base64"
                    field_schema["contentMediaType"] = "application/octet-stream"
        # Walk $defs too for nested models
        defs = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else None
        if defs:
            for _, sub in defs.items():
                _apply_base64_to_bytes(sub)

    if val_b64 and val_schema is not None:
        _apply_base64_to_bytes(val_schema)
    if ser_b64 and ser_schema is not None:
        _apply_base64_to_bytes(ser_schema)

    # Prefer the VALIDATION schema when the model is only used on the
    # input side (body / form). validation_alias fields emit under
    # their alias in validation mode and under the python name in
    # serialization mode; input-only models need the validation shape.
    # With ``separate_input_output_schemas=False``, FA also prefers the
    # validation shape when the model is used in both contexts — the
    # single emitted component reflects the input-side schema.
    used_as = _MODEL_USAGE.get(id(model_class), set())
    if used_as == {"input"} and val_schema is not None:
        json_schema = val_schema
    elif (
        not _SEPARATE_INPUT_OUTPUT
        and "input" in used_as
        and val_schema is not None
    ):
        json_schema = val_schema
    else:
        json_schema = ser_schema if ser_schema is not None else val_schema
    if json_schema is None:
        return

    # Promote nested $defs into components/schemas, normalising `Name[T]`
    # generic names to the `Name_T_` form FastAPI uses.
    if "$defs" in json_schema:
        for name, defn in json_schema["$defs"].items():
            schemas.setdefault(_normalize_model_name(name), defn)

    raw_name = getattr(model_class, "__name__", None)
    model_name = _normalize_model_name(raw_name) if raw_name else None
    used_as = _MODEL_USAGE.get(id(model_class), set())
    is_self_recursive = _model_is_self_recursive(model_class)
    _differ = _val_ser_schemas_differ(model_class)
    _has_cf = _model_has_computed_fields(model_class)
    _allow_split_here = _SEPARATE_INPUT_OUTPUT or _has_cf
    if (
        _allow_split_here
        and model_name and ("input" in used_as and "output" in used_as)
        and (is_self_recursive or _differ)
    ):
        # Self-recursive + dual-use — FastAPI emits split `-Input`/`-Output`
        # variants so body vs response can differ (the schemas are usually
        # identical, but the two distinct refs let the spec read cleanly
        # when tools traverse them separately).
        for mname, ms in (
            (f"{model_name}-Input", val_schema),
            (f"{model_name}-Output", ser_schema),
        ):
            if ms is None:
                continue
            clean_split = {k: v for k, v in ms.items() if k != "$defs"}
            schemas.setdefault(mname, clean_split)
            if "$defs" in ms:
                for n, d in ms["$defs"].items():
                    schemas.setdefault(_normalize_model_name(n), d)
        return

    if model_name and model_name not in schemas:
        # Build a clean schema without $defs (they live in components/schemas)
        clean = {k: v for k, v in json_schema.items() if k != "$defs"}
        # Rewrite internal $defs references to component $ref paths
        clean = _rewrite_defs_refs(clean)
        schemas[model_name] = clean


def _collect_security_schemes(
    route: dict[str, Any], security_schemes: dict[str, Any]
) -> None:
    """Extract security schemes from route dependency parameters.

    Checks both ``params`` and ``_all_params`` (the latter preserves
    dependency params even after handler compilation strips them).
    Prefers ``_original_dep_callable`` because ``dep_callable`` is often
    a sync-wrapper that doesn't carry the original security-scheme's
    ``.model`` attribute.
    """
    all_params = route.get("_all_params", route.get("params", []))
    for param in all_params:
        # Check original first — sync wrappers hide the .model attribute
        dep_callable = param.get("_original_dep_callable") or param.get("dep_callable")
        if dep_callable is None:
            continue

        obj = dep_callable
        if hasattr(obj, "model") and isinstance(obj.model, dict):
            scheme_name = getattr(obj, "scheme_name", None) or type(obj).__name__
            if scheme_name not in security_schemes:
                security_schemes[scheme_name] = obj.model


def _derive_security_from_deps(route: dict[str, Any]) -> list[dict[str, list[str]]]:
    """Auto-derive operation security list from detected security-scheme
    deps.

    FastAPI emits `{scheme_name: []}` by default — scopes are populated
    only when the user used `Security(scheme, scopes=["read", ...])`. A
    scheme's own scope catalog (`OAuth2PasswordBearer(scopes={...})`)
    advertises what scopes exist, not what each endpoint requires.
    """
    all_params = route.get("_all_params", route.get("params", []))
    out: list[dict[str, list[str]]] = []
    seen: set[str] = set()
    for param in all_params:
        dep_callable = param.get("_original_dep_callable") or param.get("dep_callable")
        if dep_callable is None:
            continue
        if hasattr(dep_callable, "model") and isinstance(dep_callable.model, dict):
            scheme_name = getattr(dep_callable, "scheme_name", None) or type(dep_callable).__name__
            if scheme_name in seen:
                continue
            seen.add(scheme_name)
            # Scopes are the scopes REQUIRED by this specific Security()
            # call (empty list = just "must be authenticated"). Our resolver
            # stashes the user-supplied scopes on the dep step under
            # `_security_scopes` when it sees `Security(scheme, scopes=...)`.
            # Top-level handler params additionally carry
            # ``_security_scopes_top`` (set by _introspect when a
            # ``Security(scheme, scopes=[...])`` marker is attached).
            scopes = (
                list(param.get("_security_scopes") or [])
                or list(param.get("_security_scopes_top") or [])
            )
            out.append({scheme_name: scopes})
    return out


def _build_callbacks(callbacks: list) -> dict[str, Any]:
    """Render OpenAPI callbacks.

    FA accepts TWO forms here:
      1. list of ``APIRoute`` — routes directly (``callbacks=router.routes``).
      2. list of ``APIRouter`` — full routers (less common).
    Both are rendered as ``{callback_name: {path: {method: operation}}}``.
    """
    from fastapi_rs.routing import APIRouter, APIRoute

    def _route_entry(cb_route, prefix: str = "") -> tuple[str, str, dict[str, Any]]:
        full_path = prefix + cb_route.path
        # Introspect the callback endpoint to surface its body/response
        # schemas (FA renders the full operation shape for callbacks —
        # requestBody + responses + 422, same as a regular route).
        cb_params: list[dict[str, Any]] = []
        try:
            from fastapi_rs._introspect import introspect_endpoint as _ie
            cb_params = _ie(cb_route.endpoint, full_path) or []
            # Mark them as handler-side params so the 422 emission logic
            # includes them (FA treats callback endpoint params the same
            # as regular-route handler params).
            for _p in cb_params:
                _p.setdefault("_is_handler_param", True)
        except Exception:  # noqa: BLE001
            cb_params = []
        cb_route_dict = {
            "path": full_path,
            "methods": cb_route.methods,
            "handler_name": cb_route.name,
            "params": cb_params,
            "_all_params": cb_params,
            "tags": cb_route.tags,
            "summary": cb_route.summary,
            "description": cb_route.description,
            "status_code": cb_route.status_code or 200,
            "response_description": cb_route.response_description,
            "responses": cb_route.responses,
            "response_model": getattr(cb_route, "response_model", None),
            "response_class": getattr(cb_route, "response_class", None),
            "deprecated": cb_route.deprecated,
            "operation_id": cb_route.operation_id,
            "include_in_schema": cb_route.include_in_schema,
            "openapi_extra": cb_route.openapi_extra,
        }
        methods_dict: dict[str, Any] = {}
        for method in cb_route.methods:
            methods_dict[method.lower()] = _build_operation(cb_route_dict, method.lower())
        return cb_route.name, full_path, methods_dict

    result: dict[str, Any] = {}
    for idx, cb in enumerate(callbacks):
        if isinstance(cb, APIRouter):
            cb_name = getattr(cb, "name", None) or f"callback_{idx}"
            paths: dict[str, Any] = {}
            for cb_route in cb.routes:
                _n, full_path, methods_dict = _route_entry(cb_route, prefix=cb.prefix)
                paths[full_path] = methods_dict
            result[cb_name] = paths
        elif isinstance(cb, APIRoute):
            name, full_path, methods_dict = _route_entry(cb)
            result.setdefault(name or f"callback_{idx}", {})[full_path] = methods_dict
    return result


def _walk_callback_models(callbacks: list):
    """Yield every ``BaseModel`` reachable from any callback route so the
    ``_register_model`` pass picks them up and emits the component schema.
    Covers ``responses[*].model`` AND the endpoint's return annotation
    (``response_model``) AND any body/form Pydantic model in the
    endpoint signature.
    """
    from fastapi_rs.routing import APIRouter, APIRoute

    def _from_route(cb_route):
        for resp_info in (getattr(cb_route, "responses", None) or {}).values():
            if isinstance(resp_info, dict):
                m = resp_info.get("model")
                if m is not None:
                    yield m
        # Response model (return annotation)
        rm = getattr(cb_route, "response_model", None)
        if rm is not None:
            yield rm
        # Body/form models from endpoint signature. Use our introspection
        # pipeline so the inner Pydantic model surfaces.
        try:
            from fastapi_rs._introspect import introspect_endpoint as _ie
            cb_params = _ie(cb_route.endpoint, cb_route.path) or []
            for p in cb_params:
                mc = p.get("model_class")
                if mc is not None:
                    yield mc
                pmc = p.get("_param_model_class")
                if pmc is not None:
                    yield pmc
        except Exception:  # noqa: BLE001
            pass

    for cb in callbacks or []:
        if isinstance(cb, APIRouter):
            for cb_route in cb.routes:
                yield from _from_route(cb_route)
        elif isinstance(cb, APIRoute):
            yield from _from_route(cb)


def _type_hint_to_schema(type_hint: str, inner_annotation=None) -> dict[str, Any]:
    """Map a simple type-hint string to an OpenAPI schema fragment.

    When ``type_hint`` is ``list_<inner>`` and ``inner_annotation`` is the
    original inner Python type (e.g. ``UUID``/``datetime``), we emit the
    matching JSON-Schema ``items`` (``{type: string, format: uuid}`` /
    ``{type: string, format: date-time}``) to match FastAPI.
    """
    mapping: dict[str, dict[str, Any]] = {
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "str": {"type": "string"},
        "bytes": {"type": "string", "format": "binary"},
    }
    # `list_<inner>` — FastAPI emits `{type: "array", items: {type: ...}}`
    if type_hint.startswith("list_"):
        inner = type_hint[5:]
        item_schema: dict[str, Any]
        # Prefer the annotation-derived schema when available so `UUID`
        # / `datetime` items land with their proper `format`.
        rich = _schema_for_annotation(inner_annotation) if inner_annotation is not None else None
        if rich is not None:
            item_schema = rich
        else:
            item_schema = mapping.get(inner, {"type": "string"})
        return {"type": "array", "items": item_schema}
    return dict(mapping.get(type_hint, {"type": "string"}))


def _schema_for_annotation(annotation) -> dict[str, Any] | None:
    """Best-effort inline JSON schema fragment for a Python type annotation.

    Used when the parameter type carries richer schema info than the plain
    `type_hint` string can convey — e.g. `UUID`, `datetime`, `HttpUrl`.
    Falls back to `None` so the caller can use the existing mapping.
    """
    import datetime as _dt
    import uuid as _uuid
    from decimal import Decimal as _Dec
    format_map = {
        _uuid.UUID: {"type": "string", "format": "uuid"},
        _dt.datetime: {"type": "string", "format": "date-time"},
        _dt.date: {"type": "string", "format": "date"},
        _dt.time: {"type": "string", "format": "time"},
        _dt.timedelta: {"type": "string", "format": "duration"},
        _Dec: {"type": "string", "format": "decimal"},
    }
    if annotation in format_map:
        return dict(format_map[annotation])
    # For Pydantic-special types (HttpUrl, AnyUrl, EmailStr, SecretStr,
    # Base64Bytes, etc.), fall back to a TypeAdapter-derived schema. The
    # adapter produces ``{type: string, format: uri, minLength: 1,
    # maxLength: 2083}`` for ``HttpUrl`` — byte-matching FA's output.
    try:
        _mod = getattr(annotation, "__module__", "") or ""
        _is_pydantic = _mod.startswith("pydantic") or _mod.startswith("annotated_types")
        if _is_pydantic:
            from pydantic import TypeAdapter as _TA
            sch = _TA(annotation).json_schema()
            # Strip any ``$defs`` — we only want the concrete fragment.
            if isinstance(sch, dict) and "$defs" not in sch and "$ref" not in sch:
                # Don't override if this came back as an object/ref —
                # those should go through the normal model registration
                # path, not be inlined as parameter schemas.
                if sch.get("type") in ("string", "integer", "number", "boolean"):
                    return sch
    except Exception:  # noqa: BLE001
        pass
    return None
