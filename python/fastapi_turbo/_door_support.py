"""Door-support helpers — the slice of introspection the RUST DOOR and the pivot
adapter depend on, relocated out of the (clone) _introspect.py so the clone module
becomes deletable and the adapter no longer imports from it (breaks a circular
import). KEPT permanently: the Rust engine calls _make_fa_body_validator at route
build, and the adapter + clone introspection call _get_type_name / _is_union_origin
/ _TypeAdapterProxy / _FABodyValidator.
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from typing import Any


def _is_union_origin(origin: Any) -> bool:
    return origin is typing.Union or origin is types.UnionType

def _get_type_name(annotation) -> str:
    """Map a type annotation to a simple string the Rust side understands.

    Returned values:
      - "int", "float", "bool", "str", "bytes"  — scalar coercion
      - "list_int", "list_float", "list_bool", "list_str"  — multi-value
        query/form/header repetition collected into a Python list. The
        downstream router uses ``_get_container_type`` on the same
        annotation to decide whether to wrap the list in ``set`` /
        ``frozenset`` / ``tuple`` before handing it to the handler.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return "str"

    origin = typing.get_origin(annotation) or getattr(annotation, "__origin__", None)
    if origin is not None:
        # Optional[X] / Union[X, None] — strip None, recurse
        if _is_union_origin(origin):
            args = [a for a in annotation.__args__ if a is not type(None)]
            if args:
                return _get_type_name(args[0])
            return "str"
        # list[X] / List[X] / tuple[X, ...] / set[X] / frozenset[X]
        if origin in (list, tuple, set, frozenset):
            args = typing.get_args(annotation) or getattr(annotation, "__args__", ())
            inner = args[0] if args else str
            inner_name = _get_type_name(inner)
            return f"list_{inner_name}"

    # Bare ``list`` / ``tuple`` / ``set`` / ``frozenset`` (without generic
    # args) — treat as ``list_str`` so multiple query/form/header values
    # are collected into a Python list. Matches FA's behaviour for
    # ``q: Annotated[list, Query()] = []``.
    if annotation in (list, tuple, set, frozenset):
        return "list_str"

    type_map: dict[type, str] = {
        int: "int",
        float: "float",
        bool: "bool",
        str: "str",
        bytes: "bytes",
    }

    if isinstance(annotation, type):
        # Enum subclass — return the base type so Rust extracts correctly,
        # but the enum_class will be stored separately for Python-side coercion.
        import enum as _enum_mod
        if issubclass(annotation, _enum_mod.Enum):
            for base in annotation.__mro__:
                if base in type_map:
                    return type_map[base]
            return "str"
        return type_map.get(annotation, "str")

    return "str"

class _TypeAdapterProxy:
    """Minimal shim that exposes a Pydantic TypeAdapter's validator via
    `__pydantic_validator__`. The Rust body extractor caches that attribute
    at startup and calls `.validate_json(bytes)` on it per request, so this
    proxy lets non-BaseModel body types (`list[Item]`, `dict[str, X]`, …)
    follow the same hot path as a regular BaseModel.
    """

    __slots__ = (
        "_ta",
        "__pydantic_validator__",
        "__pydantic_serializer__",
        "_annotation",
    )

    def __init__(self, annotation):
        from pydantic import TypeAdapter

        self._annotation = annotation
        self._ta = TypeAdapter(annotation)
        self.__pydantic_validator__ = self._ta.validator
        self.__pydantic_serializer__ = self._ta.serializer

    def model_validate(self, value):
        return self._ta.validate_python(value)

class _FABodyValidator:
    """FA-compatible body validator: parses JSON bytes then calls
    ``validator.validate_python(data, from_attributes=True)`` on a
    TypeAdapter wrapped in ``Annotated[T, FieldInfo(annotation=T)]``.

    This mirrors how stock FastAPI runs body validation and yields the
    exact error shape (``model_attributes_type`` instead of
    ``model_type``, "Input should be a valid dictionary or object to
    extract fields from" messages, no ``ctx.class_name`` field, …).
    Exposes ``validate_json`` so the Rust router can call it
    polymorphically alongside Pydantic's own SchemaValidator.
    """

    __slots__ = ("_validator", "_is_model", "_combined_body_fields", "_native")

    def __init__(self, annotation, combined_body_fields=None):
        from pydantic import TypeAdapter
        from pydantic.fields import FieldInfo
        from typing import Annotated as _Annotated

        try:
            from pydantic import BaseModel as _BM
            is_model = isinstance(annotation, type) and issubclass(annotation, _BM)
        except ImportError:
            is_model = False

        if is_model:
            ta = TypeAdapter(_Annotated[annotation, FieldInfo(annotation=annotation)])
        else:
            ta = TypeAdapter(annotation)
        self._validator = ta.validator
        self._is_model = is_model
        # Fused happy-path validator: a real BaseModel exposes
        # ``__pydantic_validator__`` whose ``validate_json(bytes)`` does a
        # single jiter parse + validate pass (no json.loads + validate_python
        # two-pass). For a JSON object body this is byte-identical to the
        # wrapped ``validate_python(json.loads(body), from_attributes=True)``
        # path (``from_attributes`` only affects non-dict inputs). Left None
        # for ``_TypeAdapterProxy``/dataclass/scalar annotations that lack it,
        # and never used when this is a combined-body wrapper (the cold path
        # owns the per-field ``missing`` shaping for non-dict bodies).
        if combined_body_fields:
            self._native = None
        else:
            self._native = getattr(annotation, "__pydantic_validator__", None)
        # When set, this is a ``_combined_body`` wrapper — a list of
        # field names that produce per-field ``missing`` errors when
        # the raw body isn't a dict (FA parity — ``PUT`` with body
        # ``[]`` on a multi-body endpoint emits one missing per
        # required param, not ``model_attributes_type``).
        self._combined_body_fields = combined_body_fields

    def _maybe_combined_body_missing(self, data) -> None:
        if not self._combined_body_fields or isinstance(data, dict):
            return
        from pydantic_core import InitErrorDetails, PydanticCustomError
        from pydantic import ValidationError
        details = [
            InitErrorDetails(
                type=PydanticCustomError("missing", "Field required", {}),
                loc=(fname,),
                input=None,
            )
            for fname in self._combined_body_fields
        ]
        raise ValidationError.from_exception_data(
            "combined_body_missing", details
        ) from None

    def validate_json(self, body):
        import json as _json

        # Happy-path fast lane: a real BaseModel's fused jiter validator
        # parses + validates in one pass. On ANY exception fall through to
        # the existing two-pass path below, which preserves FA-exact error
        # shapes (json_invalid loc=byte_pos, model_attributes_type for
        # non-object bodies, combined-body per-field missing).
        if self._native is not None:
            try:
                if isinstance(body, (bytes, bytearray)):
                    return self._native.validate_json(body)
                if isinstance(body, memoryview):
                    return self._native.validate_json(bytes(body))
                if isinstance(body, str):
                    return self._native.validate_json(body)
            except Exception:  # noqa: BLE001
                pass

        if isinstance(body, (bytes, bytearray, memoryview)):
            raw = bytes(body)
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            self._maybe_combined_body_missing(body)
            return self._validator.validate_python(body, from_attributes=True)
        try:
            data = _json.loads(raw)
        except Exception as e:
            # FA parity: a non-JSONDecodeError from json.loads (e.g. a
            # test patch raising a bare Exception) still maps to a 400
            # "There was an error parsing the body" response — FastAPI's
            # request body handler catches the generic exception the
            # same way.
            if not isinstance(e, _json.JSONDecodeError):
                from fastapi_turbo.exceptions import HTTPException as _HE
                raise _HE(status_code=400, detail="There was an error parsing the body") from None
            # Match FastAPI: emit a single `json_invalid` error with
            # loc=("body", byte_pos) and ctx.error = Python's json
            # message. We raise a synthetic ValidationError so the Rust
            # error formatter handles it uniformly.
            from pydantic_core import InitErrorDetails, PydanticCustomError
            from pydantic import ValidationError
            raise ValidationError.from_exception_data(
                "json_invalid",
                [
                    InitErrorDetails(
                        type=PydanticCustomError(
                            "json_invalid",
                            "JSON decode error",
                            {"error": e.msg},
                        ),
                        loc=(e.pos,),
                        input={},
                    )
                ],
            ) from None
        self._maybe_combined_body_missing(data)
        return self._validator.validate_python(data, from_attributes=True)

    def validate_python(self, value):
        self._maybe_combined_body_missing(value)
        return self._validator.validate_python(value, from_attributes=True)

def _make_fa_body_validator(annotation, combined_body_fields=None):
    # If a ``_TypeAdapterProxy`` was handed in (set by the body-type
    # resolver for ``list[Item]`` / ``dict[str, X]`` / scalar body),
    # unwrap it back to the raw annotation so the FA-style adapter is
    # built on the real type.
    if isinstance(annotation, _TypeAdapterProxy):
        annotation = annotation._annotation
    # If the model is a combined-body wrapper (built by
    # ``_maybe_embed_body_params``), pick up its required field names so
    # validation emits per-field ``missing`` errors instead of a single
    # ``model_attributes_type`` when the raw body isn't a dict.
    if combined_body_fields is None:
        fields = getattr(annotation, "_fastapi_turbo_combined_body_fields", None)
        if fields:
            combined_body_fields = tuple(fields)
    try:
        return _FABodyValidator(annotation, combined_body_fields=combined_body_fields)
    except Exception:  # noqa: BLE001
        return None


# ── Async-handler driving glue (relocated from _resolution.py) ──────────────
# Door glue, NOT introspection: the door drives an ``async def`` handler/dep from
# sync code (block_in_place → with_gil). Kept here so _resolution.py (clone
# introspection) can be deleted while this survives.


def _has_await_in_source(func) -> bool:
    """Best-effort static check: does this function's OWN body contain any
    ``await`` expressions? Nested function/lambda bodies are skipped — an
    ``await`` inside a nested ``async def`` (e.g. a streaming generator the
    handler builds and returns) executes only when that inner object is
    driven, never while the handler itself runs, so it must not misclassify
    an await-free handler onto the worker-submit path (an Event round trip
    per request). This matches code-object semantics exactly: a coroutine
    can only suspend on an await/async-for/async-with compiled into its own
    code object, and nested defs are separate code objects. Returns True on
    any detection failure so greenlet-bridge libs (SQLAlchemy async,
    redis.asyncio) fall through to the safe path.

    NOTE: ``_uses_running_loop`` deliberately KEEPS whole-source semantics —
    loop-affinity primitives (create_task, get_running_loop, ...) are
    loop-binding even when they hide inside nested code that runs later."""
    try:
        import ast
        import inspect as _inspect
        import textwrap

        src = _inspect.getsource(func)
        tree = ast.parse(textwrap.dedent(src))
        root = tree.body[0]
        if not isinstance(root, (ast.AsyncFunctionDef, ast.FunctionDef)):
            return True
        stack = list(ast.iter_child_nodes(root))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                return True
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef, ast.Lambda)):
                continue  # nested def: its awaits run when IT is driven, not now
            stack.extend(ast.iter_child_nodes(node))
        return False
    except Exception:  # noqa: BLE001
        return True


# asyncio primitives that REQUIRE a running loop bound to the *calling* thread.
_LOOP_AFFINITY_NAMES = frozenset(
    {
        "get_running_loop",
        "get_event_loop",
        "current_task",
        "all_tasks",
        "create_task",
        "ensure_future",
        "run_coroutine_threadsafe",
        "call_soon",
        "call_soon_threadsafe",
        "call_later",
        "call_at",
    }
)


def _uses_running_loop(func) -> bool:
    """Best-effort static check: does this function reference an asyncio primitive
    that needs a running event loop on the calling thread? Returns False on any
    detection failure (the ``await`` scan already errs safe by returning True)."""
    try:
        import ast
        import inspect as _inspect
        import textwrap

        src = _inspect.getsource(func)
        tree = ast.parse(textwrap.dedent(src))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _LOOP_AFFINITY_NAMES:
                return True
            if isinstance(node, ast.Name) and node.id in _LOOP_AFFINITY_NAMES:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _make_sync_wrapper(async_func, *, for_handler: bool = False, app=None):
    """Wrap an async function so it can be called from sync code (the door's
    block_in_place path). No-await fast path drives it with one ``send(None)`` on
    the calling thread; otherwise it routes to the shared worker loop. See git
    history (_resolution.py) for the three execution paths in detail."""
    func_id = id(async_func)
    _orig_name = getattr(async_func, "__name__", None)
    _orig_qual = getattr(async_func, "__qualname__", None)

    def _stamp(w):
        w._fastapi_turbo_wrapped_id = func_id
        w._fastapi_turbo_original_endpoint = async_func
        if _orig_name:
            w.__name__ = _orig_name
        if _orig_qual:
            w.__qualname__ = _orig_qual
        return w

    if not _has_await_in_source(async_func) and not _uses_running_loop(async_func):

        def _noawait_caller(**kwargs):
            coro = async_func(**kwargs)
            try:
                coro.send(None)
            except StopIteration as e:
                return e.value
            except BaseException as exc:
                try:
                    coro.close()
                except Exception:  # noqa: BLE001
                    pass
                # Misclassification safety net (same double-run semantics as
                # the suspend fallback below): a loop-needing primitive raises
                # this BEFORE suspending, e.g. when ``inspect.getsource``
                # followed a ``__wrapped__`` chain to an await-free function
                # while the wrapper actually awaits. Re-drive on the worker
                # loop instead of 500ing.
                if isinstance(exc, RuntimeError) and "no running event loop" in str(
                    exc
                ):
                    from fastapi_turbo._async_worker import submit

                    return submit(async_func(**kwargs), app=app)
                raise
            try:
                coro.close()
            except Exception:  # noqa: BLE001
                pass
            from fastapi_turbo._async_worker import submit

            return submit(async_func(**kwargs), app=app)

        return _stamp(_noawait_caller)

    if for_handler:

        def _submit_caller(**kwargs):
            from fastapi_turbo._async_worker import submit

            return submit(async_func(**kwargs), app=app)

        return _stamp(_submit_caller)

    needs_loop = [False]

    def _submit_partial(coro):
        import asyncio as _asyncio

        from fastapi_turbo._async_worker import get_loop

        loop = get_loop()
        fut = _asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=30)

    def _sync_caller(**kwargs):
        if needs_loop[0]:
            from fastapi_turbo._async_worker import submit

            return submit(async_func(**kwargs), app=app)

        coro = async_func(**kwargs)
        try:
            coro.send(None)
        except StopIteration as e:
            return e.value
        except BaseException:
            needs_loop[0] = True
            try:
                coro.close()
            except Exception:  # noqa: BLE001
                pass
            from fastapi_turbo._async_worker import submit

            return submit(async_func(**kwargs), app=app)
        needs_loop[0] = True
        return _submit_partial(coro)

    return _stamp(_sync_caller)


async def _inline_runner(coro, send):
    """ASYNC_INLINE task body: complete the request INSIDE the task's final step.

    The E-path previously wired ``task.add_done_callback(completer)`` — but
    asyncio delivers done-callbacks via ``loop.call_soon``, i.e. one EXTRA
    loop pass between the handler finishing and the tokio side being woken.
    Awaiting the handler here and calling ``send`` (a Rust ``InlineSend``
    pyclass: non-blocking ``oneshot::Sender::send``) as the last statement of
    the task body delivers the outcome in the SAME loop iteration the handler
    completes in.

    ``except BaseException`` ships the exception object (CancelledError from a
    timeout/disconnect cancel included) — byte-parity with the old completer's
    ``task.result()`` re-raise, minus asyncio's "exception was never
    retrieved" hazard (the task always finishes clean).
    """
    try:
        send(await coro, None)
    except BaseException as e:  # noqa: BLE001 — shipped to Rust, re-raised there
        send(None, e)
