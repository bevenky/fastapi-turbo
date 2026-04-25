use axum::body::Body;
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyNone, PyString};
use std::path::Path;
use std::sync::OnceLock;

// ── Cached Python-side references — looked up once, reused forever ──────
//
// These OnceLocks store GIL-free pointers we can reacquire cheaply. Each
// import/getattr hop saved here is ~1 μs per request on the hot path.

/// Cached `orjson.dumps` — set on first use when orjson is importable.
static ORJSON_DUMPS: OnceLock<Py<PyAny>> = OnceLock::new();
/// Cached `dataclasses.is_dataclass` and `asdict` — avoids `py.import("dataclasses")` per call.
static DC_IS_DATACLASS: OnceLock<Py<PyAny>> = OnceLock::new();
static DC_ASDICT: OnceLock<Py<PyAny>> = OnceLock::new();

/// Cached fastapi_turbo.responses class pointers. A Python type pointer is
/// stable for the life of the interpreter, so `obj.get_type() == cached_ptr`
/// is a 1-ns comparison — ~5× faster than `obj.getattr("path")` for
/// detecting a known response subclass.
static JSON_RESPONSE_CLS: OnceLock<Py<PyAny>> = OnceLock::new();
static FILE_RESPONSE_CLS: OnceLock<Py<PyAny>> = OnceLock::new();
static PLAIN_RESPONSE_CLS: OnceLock<Py<PyAny>> = OnceLock::new();
static HTML_RESPONSE_CLS: OnceLock<Py<PyAny>> = OnceLock::new();
static STREAMING_RESPONSE_CLS: OnceLock<Py<PyAny>> = OnceLock::new();

fn init_response_classes(py: Python<'_>) {
    if JSON_RESPONSE_CLS.get().is_some() {
        return;
    }
    if let Ok(m) = py.import("fastapi_turbo.responses") {
        if let Ok(c) = m.getattr("JSONResponse") {
            let _ = JSON_RESPONSE_CLS.set(c.unbind());
        }
        if let Ok(c) = m.getattr("FileResponse") {
            let _ = FILE_RESPONSE_CLS.set(c.unbind());
        }
        if let Ok(c) = m.getattr("PlainTextResponse") {
            let _ = PLAIN_RESPONSE_CLS.set(c.unbind());
        }
        if let Ok(c) = m.getattr("HTMLResponse") {
            let _ = HTML_RESPONSE_CLS.set(c.unbind());
        }
        if let Ok(c) = m.getattr("StreamingResponse") {
            let _ = STREAMING_RESPONSE_CLS.set(c.unbind());
        }
    }
}

fn orjson_dumps(py: Python<'_>) -> Option<&'static Py<PyAny>> {
    if let Some(f) = ORJSON_DUMPS.get() {
        return Some(f);
    }
    let orjson = py.import("orjson").ok()?;
    let dumps: Py<PyAny> = orjson.getattr("dumps").ok()?.unbind();
    let _ = ORJSON_DUMPS.set(dumps);
    ORJSON_DUMPS.get()
}

fn dataclass_helpers(py: Python<'_>) -> Option<(&'static Py<PyAny>, &'static Py<PyAny>)> {
    if let (Some(f), Some(g)) = (DC_IS_DATACLASS.get(), DC_ASDICT.get()) {
        return Some((f, g));
    }
    let dc = py.import("dataclasses").ok()?;
    let is_dc: Py<PyAny> = dc.getattr("is_dataclass").ok()?.unbind();
    let asdict: Py<PyAny> = dc.getattr("asdict").ok()?.unbind();
    let _ = DC_IS_DATACLASS.set(is_dc);
    let _ = DC_ASDICT.set(asdict);
    Some((DC_IS_DATACLASS.get()?, DC_ASDICT.get()?))
}

/// Cached JSON ``default=`` callable — matches FA's
/// ``jsonable_encoder`` for the common types orjson can't natively
/// handle: ``Decimal`` → str, ``bytes`` → UTF-8 str, ``BaseModel`` →
/// dict via ``model_dump``, falls back to ``str(obj)`` like FA.
static JSON_DEFAULT: OnceLock<Py<PyAny>> = OnceLock::new();

fn json_default(py: Python<'_>) -> &'static Py<PyAny> {
    JSON_DEFAULT.get_or_init(|| {
        py.import("fastapi_turbo.responses")
            .and_then(|m| m.getattr("_json_default"))
            .expect("fastapi_turbo.responses._json_default")
            .unbind()
    })
}

/// Cached kwargs dict for orjson.dumps: ``{"default": _json_default}``.
/// Allocated once, reused forever — saves ~1-2μs per JSON response
/// vs allocating a fresh PyDict on every call.
static ORJSON_KWARGS: OnceLock<Py<PyAny>> = OnceLock::new();

fn orjson_kwargs(py: Python<'_>) -> &'static Py<PyAny> {
    ORJSON_KWARGS.get_or_init(|| {
        let d = pyo3::types::PyDict::new(py);
        d.set_item("default", json_default(py).bind(py))
            .expect("set default");
        d.unbind().into_any()
    })
}

/// Serialize a dict/list to JSON bytes via cached orjson (or stdlib fallback).
/// Uses `default=float` so `decimal.Decimal` values serialize correctly —
/// psycopg3 returns Decimal for PostgreSQL `numeric` columns.
fn dict_to_json_bytes(py: Python<'_>, obj: &Bound<'_, PyAny>) -> Vec<u8> {
    if let Some(dumps) = orjson_dumps(py) {
        let kw = orjson_kwargs(py);
        if let Ok(bytes) = dumps.call(py, (obj,), Some(kw.bind(py).cast().unwrap())) {
            if let Ok(b) = bytes.extract::<Vec<u8>>(py) {
                return b;
            }
        }
    }
    fallback_json(py, obj).into_bytes()
}

/// Convert a Python return value into an Axum `Response`.
/// Checks are ordered by frequency: dict/list first (most common), then
/// Response objects, then primitives. BaseModel and dataclass live at the
/// bottom so we don't pay their cost on every call.
pub fn py_to_response(py: Python<'_>, obj: &Bound<'_, PyAny>) -> Response {
    py_to_response_with_request(py, obj, None, None)
}

/// Request-aware variant: `range_header` is the inbound `Range:` header
/// (if any) so `FileResponse` can answer with `206 Partial Content`;
/// `if_range_header` is the inbound `If-Range:` value so the server can
/// fall back to `200` when the client's cached validator is stale
/// (RFC 7233 §3.2).
///
/// The router uses this at every handler-result conversion site; plain
/// `py_to_response()` stays valid for call sites that don't have request
/// context (e.g., recursive JSON-mode model dumps).
pub fn py_to_response_with_request(
    py: Python<'_>,
    obj: &Bound<'_, PyAny>,
    range_header: Option<&str>,
    if_range_header: Option<&str>,
) -> Response {
    // dict or list -> JSON (MOST COMMON — check first, skip attr lookups)
    if obj.is_instance_of::<PyDict>() || obj.is_instance_of::<PyList>() {
        let bytes = dict_to_json_bytes(py, obj);
        return (
            StatusCode::OK,
            [("content-type", "application/json")],
            bytes::Bytes::from(bytes),
        )
            .into_response();
    }
    // Tuples: serialize like lists (JSON array). FA emits a tuple as
    // a JSON array, so ``return (a, b)`` from a handler works.
    if obj.is_instance_of::<pyo3::types::PyTuple>() {
        let mut buf = String::new();
        write_any_json(py, obj, &mut buf);
        return (StatusCode::OK, [("content-type", "application/json")], buf).into_response();
    }

    // None -> 200 with "null" body (matches FastAPI: json-serializes None to null)
    if obj.is_instance_of::<PyNone>() {
        return (
            StatusCode::OK,
            [("content-type", "application/json")],
            "null",
        )
            .into_response();
    }

    // ── Fast-path class dispatch for known Response subclasses ──
    // Python class identity compare (~1 ns) — avoids 3-4 `getattr` calls
    // per response (~1-2 μs saved on every FileResponse / JSONResponse).
    init_response_classes(py);
    let ty = obj.get_type();

    // JSONResponse / PlainTextResponse / HTMLResponse: body is already
    // rendered bytes; skip the path + body_iterator probes entirely.
    let is_plain_response = JSON_RESPONSE_CLS.get().is_some_and(|c| ty.is(c.bind(py)))
        || PLAIN_RESPONSE_CLS.get().is_some_and(|c| ty.is(c.bind(py)))
        || HTML_RESPONSE_CLS.get().is_some_and(|c| ty.is(c.bind(py)));
    if is_plain_response {
        if let Ok(status_attr) = obj.getattr("status_code") {
            return response_object_to_response(py, obj, &status_attr);
        }
    }

    // StreamingResponse: vLLM / SGLang return a new one on every
    // chat/completions request, so its dispatch sits on the TTFB hot path.
    // Exact-class check avoids the generic getattr("status_code") →
    // getattr("path") → getattr("body_iterator") probe below.
    if STREAMING_RESPONSE_CLS
        .get()
        .is_some_and(|c| ty.is(c.bind(py)))
    {
        return crate::streaming::create_streaming_response(py, obj);
    }

    // FileResponse: skip path/media_type/headers getattr-chain if we can
    // confirm the exact class. Fall back to the generic attr-probe path
    // for unknown subclasses.
    if FILE_RESPONSE_CLS.get().is_some_and(|c| ty.is(c.bind(py))) {
        if let Ok(path_attr) = obj.getattr("path") {
            if let Ok(path_str) = path_attr.extract::<String>() {
                let media_type = obj
                    .getattr("media_type")
                    .ok()
                    .and_then(|a| a.extract::<String>().ok());
                let headers = extract_response_headers(obj);
                return if range_header.is_some() || if_range_header.is_some() {
                    file_response_with_range(
                        &path_str,
                        media_type,
                        headers,
                        range_header,
                        if_range_header,
                    )
                } else {
                    file_response(&path_str, media_type, headers)
                };
            }
        }
    }

    // Response-like object (has .status_code) — covers user-defined subclasses,
    // StreamingResponse, RedirectResponse, etc.
    if let Ok(status_attr) = obj.getattr("status_code") {
        // FileResponse subclass check — inherited `.path` attribute.
        if let Ok(path_attr) = obj.getattr("path") {
            if !path_attr.is_none() {
                if let Ok(path_str) = path_attr.extract::<String>() {
                    let media_type = obj
                        .getattr("media_type")
                        .ok()
                        .and_then(|a| a.extract::<String>().ok());
                    let headers = extract_response_headers(obj);
                    return if range_header.is_some() || if_range_header.is_some() {
                        file_response_with_range(
                            &path_str,
                            media_type,
                            headers,
                            range_header,
                            if_range_header,
                        )
                    } else {
                        file_response(&path_str, media_type, headers)
                    };
                }
            }
        }
        // StreamingResponse case: body_iterator is set to a real iterator
        if let Ok(body_iter) = obj.getattr("body_iterator") {
            if !body_iter.is_none() {
                return crate::streaming::create_streaming_response(py, obj);
            }
        }
        return response_object_to_response(py, obj, &status_attr);
    }

    // Pydantic BaseModel: call model_dump(mode="json", by_alias=True)
    // then serialize as JSON. FA runs responses through
    // ``jsonable_encoder`` which effectively invokes the same
    // JSON-mode dump — HttpUrl → string, UUID → str, datetime → ISO.
    // Checked BEFORE primitives because BaseModel instances may also
    // satisfy extract::<String>() via __str__.
    if let Ok(dump) = obj.getattr("model_dump") {
        if dump.is_callable() {
            let kwargs = PyDict::new(py);
            let _ = kwargs.set_item("by_alias", true);
            let _ = kwargs.set_item("mode", "json");
            if let Ok(dumped) = dump.call((), Some(&kwargs)) {
                return py_to_response(py, &dumped);
            }
        }
    }

    // Dataclass instance: convert via cached `dataclasses.asdict()` and recurse.
    if let Some((is_dc, asdict)) = dataclass_helpers(py) {
        if !obj.is_instance_of::<pyo3::types::PyType>() {
            if let Ok(truthy) = is_dc.call1(py, (obj,)) {
                if truthy.is_truthy(py).unwrap_or(false) {
                    if let Ok(d) = asdict.call1(py, (obj,)) {
                        return py_to_response(py, d.bind(py));
                    }
                }
            }
        }
    }

    // bool -> JSON boolean (MUST come before int — Python bool is subclass of int)
    if obj.is_instance_of::<PyBool>() {
        let value = pyobj_to_serde(py, obj);
        let body = serde_json::to_string(&value).unwrap_or_else(|_| "null".to_string());
        return (StatusCode::OK, [("content-type", "application/json")], body).into_response();
    }

    // int / float -> JSON number (matches FastAPI: all scalars are JSON-serialized)
    if obj.is_instance_of::<PyInt>() || obj.is_instance_of::<PyFloat>() {
        let value = pyobj_to_serde(py, obj);
        let body = serde_json::to_string(&value).unwrap_or_else(|_| "null".to_string());
        return (StatusCode::OK, [("content-type", "application/json")], body).into_response();
    }

    // str -> JSON-wrapped string (matches FastAPI: strings are JSON-serialized).
    // Hand-rolled escape only handled `\` + `"` — control chars (\n, \t,
    // \r, \x00..\x1f) produced invalid JSON per RFC 8259. Delegate to
    // serde_json::to_string which encodes every case correctly AND
    // escapes non-ASCII to \uXXXX when needed.
    if let Ok(s) = obj.extract::<String>() {
        let json = serde_json::to_string(&s).unwrap_or_else(|_| "\"\"".to_string());
        return (StatusCode::OK, [("content-type", "application/json")], json).into_response();
    }

    // Plain Python class with ``__dict__`` (FA dependency classes like
    // ``OAuth2PasswordRequestForm``) — serialize via ``vars(obj)`` so
    // handlers can ``return form_data`` directly. Falls through to the
    // final ``str()`` fallback for true primitives / lambdas.
    if let Ok(d) = obj.getattr("__dict__") {
        if let Ok(dict) = d.cast::<PyDict>() {
            let mut buf = String::with_capacity(64);
            write_dict_json(py, dict, &mut buf);
            return (StatusCode::OK, [("content-type", "application/json")], buf).into_response();
        }
    }

    // Fallback: str() it
    let repr = obj.str().map(|s| s.to_string()).unwrap_or_default();
    (StatusCode::OK, [("content-type", "text/plain")], repr).into_response()
}

/// Convert a Python Response-like object (has status_code, headers, body).
///
/// Hot path: pre-rendered JSONResponse (body is bytes, headers is small dict).
/// Optimizations:
///   - UTF-8 assumption (`from_utf8_unchecked`) — JSON is always valid UTF-8
///   - Skip raw_headers when list is empty (avoid getattr + iter)
///   - Skip empty body extract
fn response_object_to_response(
    py: Python<'_>,
    obj: &Bound<'_, PyAny>,
    status_attr: &Bound<'_, PyAny>,
) -> Response {
    let status_code = status_attr.extract::<u16>().unwrap_or(200);
    let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

    // Collect raw_headers keys first so we can skip them in the headers
    // dict pass (duplicates must come from raw_headers only — the dict
    // just carries the canonical latest value, which `MutableHeaders.
    // append` already pushed into raw_headers too).
    let mut raw_header_keys: std::collections::HashSet<String> = std::collections::HashSet::new();
    if let Ok(raw_attr) = obj.getattr("raw_headers") {
        if let Ok(list) = raw_attr.cast::<PyList>() {
            for item in list.iter() {
                if let Ok(tup) = item.extract::<(String, String)>() {
                    raw_header_keys.insert(tup.0.to_ascii_lowercase());
                }
            }
        }
    }

    let mut headers = HeaderMap::new();
    if let Ok(hdr_obj) = obj.getattr("headers") {
        // Support both plain dict and MutableHeaders (which has .items())
        if let Ok(dict) = hdr_obj.cast::<PyDict>() {
            if !dict.is_empty() {
                for (k, v) in dict.iter() {
                    if let (Ok(key), Ok(val)) = (k.extract::<String>(), v.extract::<String>()) {
                        if raw_header_keys.contains(&key.to_ascii_lowercase()) {
                            continue;
                        }
                        if let (Ok(hname), Ok(hval)) =
                            (HeaderName::try_from(key), HeaderValue::from_str(&val))
                        {
                            headers.insert(hname, hval);
                        }
                    }
                }
            }
        } else if let Ok(items_list) = hdr_obj.call_method0("items") {
            if let Ok(list) = items_list.cast::<pyo3::types::PyList>() {
                for item in list.iter() {
                    if let Ok((key, val)) = item.extract::<(String, String)>() {
                        if raw_header_keys.contains(&key.to_ascii_lowercase()) {
                            continue;
                        }
                        if let (Ok(hname), Ok(hval)) =
                            (HeaderName::try_from(key), HeaderValue::from_str(&val))
                        {
                            headers.insert(hname, hval);
                        }
                    }
                }
            }
        }
    }
    // raw_headers list preserves duplicates (e.g., multiple Set-Cookie).
    // Skip entirely if the list is empty (common case — no cookies set).
    if let Ok(raw_attr) = obj.getattr("raw_headers") {
        if let Ok(list) = raw_attr.cast::<PyList>() {
            if !list.is_empty() {
                for item in list.iter() {
                    if let Ok(tup) = item.extract::<(String, String)>() {
                        if let (Ok(hname), Ok(hval)) =
                            (HeaderName::try_from(tup.0), HeaderValue::from_str(&tup.1))
                        {
                            headers.append(hname, hval);
                        }
                    }
                }
            }
        }
    }

    // Body extraction: skip the String detour — go straight to Bytes / Body.
    // Saves one full-buffer copy on large JSON payloads (29 KB response was
    // 20-30 μs faster after this change).
    let body_bytes: bytes::Bytes = if let Ok(b) = obj.getattr("body") {
        if let Ok(pyb) = b.cast::<pyo3::types::PyBytes>() {
            let slice = pyb.as_bytes();
            if slice.is_empty() {
                bytes::Bytes::new()
            } else {
                bytes::Bytes::copy_from_slice(slice)
            }
        } else if let Ok(s) = b.extract::<String>() {
            bytes::Bytes::from(s.into_bytes())
        } else if let Ok(bytes) = b.extract::<Vec<u8>>() {
            bytes::Bytes::from(bytes)
        } else {
            // Fallback: dict-like body (rare) — serialize to JSON.
            let val = pyobj_to_serde(py, &b);
            bytes::Bytes::from(serde_json::to_vec(&val).unwrap_or_default())
        }
    } else {
        bytes::Bytes::new()
    };

    let mut resp = axum::response::Response::builder()
        .status(status)
        .body(axum::body::Body::from(body_bytes))
        .expect("build response");
    let hmap = resp.headers_mut();
    // `append` preserves duplicate keys (X-Dup / multi Set-Cookie). Using
    // `insert` here would collapse multi-valued headers into one, losing
    // the entries we just accumulated via raw_headers.
    for (k, v) in headers.iter() {
        hmap.append(k, v.clone());
    }

    // Drain `Response(..., background=BackgroundTask(...))` or
    // `Response.background = BackgroundTasks()`. Matches FastAPI /
    // Starlette: tasks fire after the response is sent to the client.
    if let Ok(bg) = obj.getattr("background") {
        if !bg.is_none() {
            let bg_py: Py<PyAny> = bg.unbind();
            tokio::task::spawn_blocking(move || {
                Python::attach(|py| {
                    let bound = bg_py.bind(py);
                    // BackgroundTasks has `run_sync`; bare BackgroundTask
                    // is an awaitable, so fall back to `__call__()` to run
                    // the task synchronously on this worker thread.
                    if bound.call_method0("run_sync").is_ok() {
                        return;
                    }
                    // Not a BackgroundTasks — treat as single BackgroundTask
                    // (`__call__` returns a coroutine we must drive).
                    if let Ok(coro) = bound.call0() {
                        let _ = coro.call_method1("send", (py.None(),));
                    }
                });
            });
        }
    }
    resp
}

/// Convert a PyErr into an HTTP error response.
///
/// Matches FastAPI/Starlette exactly:
///   - HTTPException (has `status_code` + `detail`) → JSON `{"detail": ...}` with that status
///   - Any other exception → plain-text `Internal Server Error` with status 500.
///     The traceback is printed to stderr (Starlette's ServerErrorMiddleware does this).
pub fn pyerr_to_response(py: Python<'_>, err: &PyErr) -> Response {
    let err_value = err.value(py);

    // HTTPException path: has explicit status_code + detail
    if let Ok(status_attr) = err_value.getattr("status_code") {
        if let Ok(status_code) = status_attr.extract::<u16>() {
            // `detail` may be a string (most common), a dict, a list, or
            // any JSON-serializable value. FastAPI embeds whatever the
            // user supplied verbatim inside `{"detail": ...}`. Coercing to
            // String here turned dict details into repr strings — fix by
            // routing through json.dumps for non-string values.
            let detail_json: serde_json::Value = if let Ok(d_attr) = err_value.getattr("detail") {
                if d_attr.is_none() {
                    serde_json::Value::String("Internal Server Error".to_string())
                } else if let Ok(s) = d_attr.extract::<String>() {
                    serde_json::Value::String(s)
                } else if let Ok(b) = d_attr.extract::<bool>() {
                    serde_json::Value::Bool(b)
                } else if let Ok(i) = d_attr.extract::<i64>() {
                    serde_json::Value::Number(i.into())
                } else {
                    py.import("json")
                        .and_then(|j| j.call_method1("dumps", (&d_attr,)))
                        .and_then(|s| s.extract::<String>())
                        .ok()
                        .and_then(|s| serde_json::from_str(&s).ok())
                        .unwrap_or_else(|| serde_json::Value::String(format!("{err}")))
                }
            } else {
                serde_json::Value::String(format!("{err}"))
            };

            let status =
                StatusCode::from_u16(status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

            // Propagate HTTPException.headers (set-cookie etc.) to the response
            let mut extra_headers: Vec<(String, String)> = Vec::new();
            if let Ok(hdrs) = err_value.getattr("headers") {
                if let Ok(dict) = hdrs.cast::<PyDict>() {
                    for (k, v) in dict.iter() {
                        if let (Ok(ks), Ok(vs)) = (k.extract::<String>(), v.extract::<String>()) {
                            extra_headers.push((ks, vs));
                        }
                    }
                }
            }

            let body = serde_json::json!({ "detail": detail_json });
            let mut resp = axum::response::Response::builder()
                .status(status)
                .header("content-type", "application/json")
                .body(axum::body::Body::from(body.to_string()))
                .expect("build response");
            for (k, v) in extra_headers {
                if let (Ok(hn), Ok(hv)) =
                    (HeaderName::try_from(k.as_str()), HeaderValue::from_str(&v))
                {
                    resp.headers_mut().append(hn, hv);
                }
            }
            return resp;
        }
    }

    // Unhandled exception → print traceback to stderr and return plain-text 500.
    err.print(py);
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        [("content-type", "text/plain; charset=utf-8")],
        "Internal Server Error",
    )
        .into_response()
}

/// Fallback JSON serialization when orjson is not available.
fn fallback_json(py: Python<'_>, obj: &Bound<'_, PyAny>) -> String {
    if let Ok(dict) = obj.cast::<PyDict>() {
        let mut buf = String::with_capacity(128);
        write_dict_json(py, dict, &mut buf);
        buf
    } else if let Ok(list) = obj.cast::<PyList>() {
        let mut buf = String::with_capacity(128);
        write_list_json(py, list, &mut buf);
        buf
    } else {
        let value = pyobj_to_serde(py, obj);
        serde_json::to_string(&value).unwrap_or_else(|_| "null".to_string())
    }
}

// ── Direct PyDict → JSON writer (zero intermediate allocations) ──────

/// Write a Python dict directly to a JSON string buffer.
/// Skips creating intermediate serde_json::Value — writes JSON bytes while walking the dict.
fn write_dict_json(py: Python<'_>, dict: &Bound<'_, PyDict>, buf: &mut String) {
    buf.push('{');
    let mut first = true;
    for (k, v) in dict.iter() {
        if !first {
            buf.push(',');
        }
        first = false;
        // Key — always a string
        buf.push('"');
        if let Ok(s) = k.cast::<PyString>() {
            json_escape_to(s.to_str().unwrap_or(""), buf);
        } else {
            let s = k.str().map(|s| s.to_string()).unwrap_or_default();
            json_escape_to(&s, buf);
        }
        buf.push_str("\":");
        // Value
        write_any_json(py, &v, buf);
    }
    buf.push('}');
}

/// Write a Python list directly to a JSON string buffer.
fn write_list_json(py: Python<'_>, list: &Bound<'_, PyList>, buf: &mut String) {
    buf.push('[');
    for (i, item) in list.iter().enumerate() {
        if i > 0 {
            buf.push(',');
        }
        write_any_json(py, &item, buf);
    }
    buf.push(']');
}

/// Write any Python value as JSON to the buffer.
#[inline]
fn write_any_json(py: Python<'_>, obj: &Bound<'_, PyAny>, buf: &mut String) {
    if obj.is_none() {
        buf.push_str("null");
        return;
    }
    // bool MUST come before int (Python bool is subclass of int)
    if let Ok(b) = obj.cast::<PyBool>() {
        buf.push_str(if b.is_true() { "true" } else { "false" });
        return;
    }
    // ``decimal.Decimal`` MUST come before int/float extraction so
    // non-finite Decimals (Infinity / NaN) turn into JSON ``null``
    // (matching FastAPI's ``jsonable_encoder``). Finite Decimals
    // serialise as JSON **numbers** (int when the value is integral,
    // float otherwise) — that's what upstream FA emits via its default
    // encoder, and bytes-for-bytes compatibility matters for clients
    // deserialising into strict numeric types. Class-by-name check
    // keeps this cheap.
    {
        let ty = obj.get_type();
        if ty.name().map(|n| n == "Decimal").unwrap_or(false) {
            use std::fmt::Write;
            // is_finite?
            let finite = obj
                .call_method0("is_finite")
                .ok()
                .and_then(|v| v.extract::<bool>().ok())
                .unwrap_or(false);
            if !finite {
                buf.push_str("null");
                return;
            }
            // integral? -> int; else float.
            let is_integral = obj
                .call_method0("to_integral_value")
                .and_then(|tv| obj.eq(&tv))
                .ok()
                .unwrap_or(false);
            if is_integral {
                if let Ok(i) = obj.call_method0("__int__").and_then(|v| v.extract::<i64>()) {
                    let _ = write!(buf, "{i}");
                    return;
                }
                // Fall through to string form on overflow — keeps
                // precision rather than losing bits.
                if let Ok(s) = obj.str().map(|s| s.to_string()) {
                    buf.push_str(&s);
                    return;
                }
            }
            if let Ok(f) = obj
                .call_method0("__float__")
                .and_then(|v| v.extract::<f64>())
            {
                if f.is_finite() {
                    let _ = write!(buf, "{f}");
                    return;
                }
            }
            buf.push_str("null");
            return;
        }
    }
    if let Ok(i) = obj.extract::<i64>() {
        use std::fmt::Write;
        let _ = write!(buf, "{i}");
        return;
    }
    if let Ok(f) = obj.extract::<f64>() {
        use std::fmt::Write;
        if f.is_finite() {
            let _ = write!(buf, "{f}");
        } else {
            buf.push_str("null");
        }
        return;
    }
    if let Ok(s) = obj.cast::<PyString>() {
        buf.push('"');
        json_escape_to(s.to_str().unwrap_or(""), buf);
        buf.push('"');
        return;
    }
    if let Ok(dict) = obj.cast::<PyDict>() {
        write_dict_json(py, dict, buf);
        return;
    }
    if let Ok(list) = obj.cast::<PyList>() {
        write_list_json(py, list, buf);
        return;
    }
    if let Ok(tup) = obj.cast::<pyo3::types::PyTuple>() {
        buf.push('[');
        let mut first = true;
        for item in tup.iter() {
            if !first {
                buf.push(',');
            }
            first = false;
            write_any_json(py, &item, buf);
        }
        buf.push(']');
        return;
    }
    if obj.is_instance_of::<pyo3::types::PySet>()
        || obj.is_instance_of::<pyo3::types::PyFrozenSet>()
    {
        buf.push('[');
        let mut first = true;
        if let Ok(iter) = obj.try_iter() {
            for item in iter.flatten() {
                if !first {
                    buf.push(',');
                }
                first = false;
                write_any_json(py, &item, buf);
            }
        }
        buf.push(']');
        return;
    }
    // ``bytes`` / ``bytearray`` — UTF-8 decode to a JSON string. FA's
    // ``jsonable_encoder`` does the same; without this handlers that
    // return uploaded file contents as ``bytes`` serialize as the
    // Python repr (``"b'foo'"``) instead of the decoded value.
    if let Ok(b) = obj.cast::<pyo3::types::PyBytes>() {
        buf.push('"');
        let s = String::from_utf8_lossy(b.as_bytes());
        json_escape_to(&s, buf);
        buf.push('"');
        return;
    }
    if let Ok(b) = obj.cast::<pyo3::types::PyByteArray>() {
        buf.push('"');
        let data = unsafe { b.as_bytes() };
        let s = String::from_utf8_lossy(data);
        json_escape_to(&s, buf);
        buf.push('"');
        return;
    }
    // Pydantic BaseModel: ``obj.model_dump(mode="json", by_alias=True)``
    // yields a JSON-ready dict. Required for nested models
    // (e.g. ``{"item": Item(...)}``) so HttpUrl → string, UUID → str,
    // datetime → ISO. ``mode="json"`` matches ``jsonable_encoder``.
    if let Ok(dump) = obj.getattr("model_dump") {
        if dump.is_callable() {
            let kwargs = PyDict::new(py);
            let _ = kwargs.set_item("by_alias", true);
            let _ = kwargs.set_item("mode", "json");
            if let Ok(dumped) = dump.call((), Some(&kwargs)) {
                if let Ok(dict) = dumped.cast::<PyDict>() {
                    write_dict_json(py, dict, buf);
                    return;
                }
                if let Ok(list) = dumped.cast::<PyList>() {
                    write_list_json(py, list, buf);
                    return;
                }
                write_any_json(py, &dumped, buf);
                return;
            }
        }
    }
    // FA-parity: datetime / date / time expose ``.isoformat()``.
    // timedelta → total_seconds() as JSON number. UUID → str().
    // ``jsonable_encoder`` folds all of these; we replicate the most
    // common ones so handlers returning raw datetimes in a dict emit
    // ISO strings rather than Python ``str(dt)`` output.
    {
        let ty = obj.get_type();
        let name_owned: Option<String> = ty.name().ok().and_then(|n| n.extract::<String>().ok());
        if let Some(name) = name_owned.as_deref() {
            match name {
                "datetime" | "date" | "time" => {
                    if let Ok(iso) = obj.call_method0("isoformat") {
                        if let Ok(s) = iso.extract::<String>() {
                            buf.push('"');
                            json_escape_to(&s, buf);
                            buf.push('"');
                            return;
                        }
                    }
                }
                "timedelta" => {
                    if let Ok(secs) = obj.call_method0("total_seconds") {
                        if let Ok(f) = secs.extract::<f64>() {
                            use std::fmt::Write;
                            if f.is_finite() {
                                // FA emits integers when whole; mimic.
                                if f.fract() == 0.0 {
                                    let _ = write!(buf, "{}", f as i64);
                                } else {
                                    let _ = write!(buf, "{f}");
                                }
                            } else {
                                buf.push_str("null");
                            }
                            return;
                        }
                    }
                }
                "UUID" => {
                    if let Ok(s) = obj.str() {
                        buf.push('"');
                        json_escape_to(&s.to_string(), buf);
                        buf.push('"');
                        return;
                    }
                }
                _ => {}
            }
        }
    }
    // ``@dataclass`` / FA dependency classes
    // (``OAuth2PasswordRequestForm`` etc.) have no ``model_dump`` but
    // do have ``__dict__``. Serialize via ``vars(obj)`` so handlers
    // can ``return form_data`` directly — FA's ``jsonable_encoder``
    // does the same.
    if let Ok(d) = obj.getattr("__dict__") {
        if let Ok(dict) = d.cast::<PyDict>() {
            write_dict_json(py, dict, buf);
            return;
        }
    }
    // Fallback: str()
    buf.push('"');
    let s = obj.str().map(|s| s.to_string()).unwrap_or_default();
    json_escape_to(&s, buf);
    buf.push('"');
}

/// Escape a string for JSON output (handles \", \\, \n, \r, \t, control chars).
#[inline]
fn json_escape_to(s: &str, buf: &mut String) {
    for c in s.chars() {
        match c {
            '"' => buf.push_str("\\\""),
            '\\' => buf.push_str("\\\\"),
            '\n' => buf.push_str("\\n"),
            '\r' => buf.push_str("\\r"),
            '\t' => buf.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                use std::fmt::Write;
                let _ = write!(buf, "\\u{:04x}", c as u32);
            }
            c => buf.push(c),
        }
    }
}

/// Recursively convert a Python object to a `serde_json::Value`.
pub fn pyobj_to_serde(py: Python<'_>, obj: &Bound<'_, PyAny>) -> serde_json::Value {
    if obj.is_none() {
        return serde_json::Value::Null;
    }
    // bool MUST come before int because Python bool is a subclass of int
    if let Ok(b) = obj.cast::<PyBool>() {
        return serde_json::Value::Bool(b.is_true());
    }
    if let Ok(i) = obj.extract::<i64>() {
        return serde_json::Value::Number(i.into());
    }
    if let Ok(f) = obj.extract::<f64>() {
        if let Some(n) = serde_json::Number::from_f64(f) {
            return serde_json::Value::Number(n);
        }
        return serde_json::Value::Null;
    }
    if let Ok(s) = obj.extract::<String>() {
        return serde_json::Value::String(s);
    }
    if let Ok(dict) = obj.cast::<PyDict>() {
        let mut map = serde_json::Map::new();
        for (k, v) in dict.iter() {
            let key = k.str().map(|s| s.to_string()).unwrap_or_default();
            map.insert(key, pyobj_to_serde(py, &v));
        }
        return serde_json::Value::Object(map);
    }
    if let Ok(list) = obj.cast::<PyList>() {
        let arr: Vec<serde_json::Value> =
            list.iter().map(|item| pyobj_to_serde(py, &item)).collect();
        return serde_json::Value::Array(arr);
    }
    // Fallback: convert via str()
    let s = obj.str().map(|s| s.to_string()).unwrap_or_default();
    serde_json::Value::String(s)
}

/// Convert a `serde_json::Value` into a Python object.
pub fn serde_to_pyobj(py: Python<'_>, val: &serde_json::Value) -> Py<PyAny> {
    match val {
        serde_json::Value::Null => py.None(),
        serde_json::Value::Bool(b) => PyBool::new(py, *b).to_owned().into_any().unbind(),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.into_pyobject(py).expect("int").into_any().unbind()
            } else if let Some(f) = n.as_f64() {
                f.into_pyobject(py).expect("float").into_any().unbind()
            } else {
                py.None()
            }
        }
        serde_json::Value::String(s) => s.into_pyobject(py).expect("str").into_any().unbind(),
        serde_json::Value::Array(arr) => {
            let list = pyo3::types::PyList::empty(py);
            for item in arr {
                list.append(serde_to_pyobj(py, item)).ok();
            }
            list.into_any().unbind()
        }
        serde_json::Value::Object(map) => {
            let dict = PyDict::new(py);
            for (k, v) in map {
                dict.set_item(k, serde_to_pyobj(py, v)).ok();
            }
            dict.into_any().unbind()
        }
    }
}

// ── FileResponse with Range support ─────────────────────────────────

/// Extract response headers from a Python Response object into a (k, v) list.
fn extract_response_headers(obj: &Bound<'_, PyAny>) -> Vec<(String, String)> {
    let mut out = Vec::new();
    if let Ok(hdr) = obj.getattr("headers") {
        // Support both plain dict and MutableHeaders (which has .items())
        if let Ok(dict) = hdr.cast::<PyDict>() {
            for (k, v) in dict.iter() {
                if let (Ok(ks), Ok(vs)) = (k.extract::<String>(), v.extract::<String>()) {
                    out.push((ks, vs));
                }
            }
        } else if let Ok(items_list) = hdr.call_method0("items") {
            if let Ok(list) = items_list.cast::<PyList>() {
                for item in list.iter() {
                    if let Ok((ks, vs)) = item.extract::<(String, String)>() {
                        out.push((ks, vs));
                    }
                }
            }
        }
    }
    if let Ok(raw) = obj.getattr("raw_headers") {
        if let Ok(list) = raw.cast::<PyList>() {
            for item in list.iter() {
                if let Ok((k, v)) = item.extract::<(String, String)>() {
                    out.push((k, v));
                }
            }
        }
    }
    out
}

/// Serve a file from disk with Content-Type + Content-Length + Accept-Ranges.
///
/// Range request handling (206 Partial Content) is applied when the router's
/// request has a Range header — see [`file_response_with_range`] which takes
/// the range spec directly. This base helper serves the full file.
pub fn file_response(
    path_str: &str,
    media_type: Option<String>,
    extra_headers: Vec<(String, String)>,
) -> Response {
    let path = Path::new(path_str);

    // Read metadata synchronously — needed for Content-Length before
    // we start streaming the body. Catches NotFound / permission
    // errors here so the early-return 404/500 responses still work.
    let meta = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return (StatusCode::NOT_FOUND, format!("File not found: {path_str}")).into_response();
        }
        Err(e) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("File stat error: {e}"),
            )
                .into_response();
        }
    };
    if !meta.is_file() {
        // Directory / device / fifo reached FileResponse — routing
        // bug. Return 500 rather than silently serving an empty 200
        // (Starlette parity).
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("File at path {path_str} is not a file."),
        )
            .into_response();
    }
    let total_len = meta.len();

    // Small-file fast path: fully buffer if ≤ 256 KiB. Avoids the
    // per-chunk ReaderStream overhead for icon/css/json assets where
    // memory is irrelevant and throughput matters.
    const STREAM_THRESHOLD: u64 = 256 * 1024;
    let small_body: Option<Vec<u8>> = if total_len <= STREAM_THRESHOLD {
        std::fs::read(path).ok()
    } else {
        None
    };

    let mut ct = media_type.unwrap_or_else(|| "application/octet-stream".to_string());
    // FastAPI/Starlette append `; charset=utf-8` to textual types if absent.
    if (ct.starts_with("text/") || ct == "application/javascript" || ct == "application/json")
        && !ct.to_lowercase().contains("charset=")
    {
        ct.push_str("; charset=utf-8");
    }

    let body = if let Some(data) = small_body {
        Body::from(data)
    } else {
        // Large file → stream. ``tokio::fs::File::from_std`` + a
        // ``ReaderStream`` produce a ``Stream<Item = io::Result<Bytes>>``
        // that axum's body protocol consumes chunk-by-chunk — memory
        // is one chunk (default 8 KiB) per concurrent request, not
        // the full file.
        match std::fs::File::open(path) {
            Ok(std_file) => {
                let tokio_file = tokio::fs::File::from_std(std_file);
                let stream = tokio_util::io::ReaderStream::new(tokio_file);
                Body::from_stream(stream)
            }
            Err(e) => {
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("File open error: {e}"),
                )
                    .into_response();
            }
        }
    };

    let (last_modified, etag) = compute_file_stat_headers(&meta);
    let mut resp = Response::builder()
        .status(StatusCode::OK)
        .header("content-type", &ct)
        .header("content-length", total_len)
        .header("accept-ranges", "bytes")
        .header("last-modified", &last_modified)
        .header("etag", &etag)
        .body(body)
        .expect("build file response");

    for (k, v) in extra_headers {
        let k_lower = k.to_ascii_lowercase();
        // Skip headers we've already set (avoids duplicate Content-Type/Length)
        if matches!(
            k_lower.as_str(),
            "content-type" | "content-length" | "accept-ranges" | "last-modified" | "etag"
        ) {
            continue;
        }
        if let (Ok(hn), Ok(hv)) = (HeaderName::try_from(k.as_str()), HeaderValue::from_str(&v)) {
            // set-cookie must allow duplicates (append); other headers insert.
            if k_lower == "set-cookie" {
                resp.headers_mut().append(hn, hv);
            } else {
                resp.headers_mut().insert(hn, hv);
            }
        }
    }

    resp
}

/// Result of parsing an RFC 7233 `Range:` header against a known file
/// size. Mirrors Starlette 1.0's branching: ``Malformed`` → 400,
/// ``Unsatisfiable`` → 416, ``Full`` → 200 (no ranges OR DoS-cap
/// exceeded), ``Single`` → 206 single-range, ``Multi`` → 206
/// multipart/byteranges.
pub enum RangeOutcome {
    /// Serve 200 with the full body. Used when the header is absent, OR
    /// when the post-coalesce range count exceeds ``MAX_RANGES``
    /// (different-by-design DoS guard, documented in COMPATIBILITY.md).
    Full,
    /// Serve 206 with a single ``Content-Range`` header.
    Single(u64, u64),
    /// Serve 206 ``multipart/byteranges`` covering these ranges.
    Multi(Vec<(u64, u64)>),
    /// Serve 416 ``Range Not Satisfiable``.
    Unsatisfiable,
    /// Serve 400 ``Bad Request``. Carries a short reason for the body.
    Malformed(&'static str),
}

/// Parse an RFC 7233 `Range: bytes=…` header for a resource of
/// `total_len` bytes.
///
/// Supports all forms FastAPI users hit in practice:
///   * `bytes=N-M`          — absolute window
///   * `bytes=N-`           — from N through end
///   * `bytes=-N`           — last N bytes (suffix)
///   * `bytes=N-M,X-Y,...`  — multiple ranges
///
/// Logic mirrors Starlette 1.0's ``_parse_range_header`` /
/// ``_parse_ranges`` exactly:
///   * Unit must be ``bytes`` (case-insensitive token).
///   * Non-bytes unit, missing ``=``, no parseable sub-ranges → 400.
///   * Any sub-range start outside ``[0, total_len)`` → 416.
///   * Any reversed sub-range (``start > end``) → 400.
///   * Overlapping/adjacent sub-ranges are coalesced before deciding
///     single vs multipart.
///
/// DoS guard (different-by-design): post-coalesce range count > 16
/// returns ``RangeOutcome::Full`` rather than amplifying the response.
pub fn parse_byte_ranges(header: &str, total_len: u64) -> RangeOutcome {
    const MAX_RANGES: usize = 16;

    // Error-message strings match Starlette 1.0 byte-for-byte so
    // error-body comparisons against upstream pass.
    let trimmed = header.trim();
    let (unit, rest) = match trimmed.split_once('=') {
        Some((u, r)) => (u.trim(), r.trim()),
        None => return RangeOutcome::Malformed("Malformed range header."),
    };
    if !unit.eq_ignore_ascii_case("bytes") {
        return RangeOutcome::Malformed("Only support bytes range");
    }
    if total_len == 0 {
        return RangeOutcome::Unsatisfiable;
    }

    // Parse sub-ranges in Starlette's half-open ``[start, end)`` form.
    // ``start`` may be negative (``bytes=-N`` with N > total_len) which
    // the bounds check below catches.
    let mut raw: Vec<(i128, u64)> = Vec::new();
    for part in rest.split(',') {
        let part = part.trim();
        if part.is_empty() || part == "-" {
            continue;
        }
        let (a, b) = match part.split_once('-') {
            Some(pair) => pair,
            None => continue,
        };
        let a = a.trim();
        let b = b.trim();

        // Mirror Starlette's ``_parse_ranges`` per-sub-range parsing.
        // Per-sub-range ValueErrors are dropped (continue), not bailed.
        let start: i128 = if a.is_empty() {
            match b.parse::<u64>() {
                Ok(n) => total_len as i128 - n as i128,
                Err(_) => continue,
            }
        } else {
            match a.parse::<u64>() {
                Ok(s) => s as i128,
                Err(_) => continue,
            }
        };
        let end: u64 = if !a.is_empty() && !b.is_empty() {
            match b.parse::<u64>() {
                Ok(e) if e < total_len => e + 1,
                Ok(_) => total_len,
                Err(_) => continue,
            }
        } else {
            total_len
        };
        raw.push((start, end));
    }

    if raw.is_empty() {
        // Empty range value (``bytes=``) and unparseable subranges
        // (``bytes=abc-def``) both surface here in Starlette.
        return RangeOutcome::Malformed("Range header: range must be requested");
    }

    // Bounds check (BEFORE the reversed check — matches Starlette
    // order). Any start outside [0, total_len) → 416.
    if raw
        .iter()
        .any(|&(s, _)| s < 0 || (s as u128) >= total_len as u128)
    {
        return RangeOutcome::Unsatisfiable;
    }

    // Now safe to drop to u64.
    let mut ranges: Vec<(u64, u64)> = raw.iter().map(|&(s, e)| (s as u64, e)).collect();

    // Reversed in half-open form means input had start > end.
    if ranges.iter().any(|&(s, e)| s > e) {
        return RangeOutcome::Malformed("Range header: start must be less than end");
    }

    // Coalesce in half-open form. Touching: ``next.start <= prev.end``
    // (since prev.end is exclusive, touching means equal).
    ranges.sort_unstable();
    let mut coalesced: Vec<(u64, u64)> = Vec::with_capacity(ranges.len());
    for (s, e) in ranges {
        if let Some(prev) = coalesced.last_mut() {
            if s <= prev.1 {
                prev.1 = prev.1.max(e);
                continue;
            }
        }
        coalesced.push((s, e));
    }

    if coalesced.len() > MAX_RANGES {
        // Different-by-design DoS guard. Coalesce-first ensures
        // legitimate duplicate/overlapping inputs never trip this.
        return RangeOutcome::Full;
    }

    // Convert half-open back to inclusive ``[start, end]`` for the
    // caller (which builds Content-Range / wire bytes).
    let inclusive: Vec<(u64, u64)> = coalesced.into_iter().map(|(s, e)| (s, e - 1)).collect();
    if inclusive.len() == 1 {
        let (s, e) = inclusive[0];
        return RangeOutcome::Single(s, e);
    }
    RangeOutcome::Multi(inclusive)
}

/// Compute the Last-Modified and ETag pair for a file, based on its
/// ``mtime`` and ``size``. Output formats mirror upstream Starlette:
///
///   * ``Last-Modified`` is RFC 1123 GMT (``formatdate(usegmt=True)``).
///   * ``ETag`` is ``"<md5_hex>"`` where the pre-image is
///     ``"{mtime_float_str}-{size}"``. Python's ``str(float)`` produces
///     the shortest repr that round-trips; we emulate by using the
///     nanosecond-precision seconds field plus a 9-digit fractional
///     part, trimming trailing zeros but keeping at least one digit
///     after the decimal to match ``str(float)`` on whole seconds
///     (``1234567890.0``).
pub fn compute_file_stat_headers(meta: &std::fs::Metadata) -> (String, String) {
    use std::time::{SystemTime, UNIX_EPOCH};

    let mtime = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
    let dur = mtime
        .duration_since(UNIX_EPOCH)
        .unwrap_or_else(|_| std::time::Duration::from_secs(0));
    let secs = dur.as_secs();
    let nanos = dur.subsec_nanos();
    let size = meta.len();

    // Format ``mtime`` as ``str(float)`` would — shortest repr that
    // round-trips. We build a fixed-precision string then trim.
    let mtime_str = format_python_float_secs_nanos(secs, nanos);
    let etag_base = format!("{mtime_str}-{size}");

    let md5_hex = md5_hex_str(etag_base.as_bytes());
    let etag = format!("\"{md5_hex}\"");

    let last_modified = format_http_date(secs);
    (last_modified, etag)
}

/// Emulate Python's ``str(float)`` for ``secs + nanos/1e9`` — shortest
/// fractional repr that round-trips. Used for ETag pre-image parity
/// with Starlette.
fn format_python_float_secs_nanos(secs: u64, nanos: u32) -> String {
    if nanos == 0 {
        // Whole seconds — ``str(1234.0)`` → ``"1234.0"``.
        return format!("{secs}.0");
    }
    // 9-digit fractional, then trim trailing zeros (keep one digit min).
    let mut s = format!("{secs}.{nanos:09}");
    while s.ends_with('0') && !s.ends_with(".0") {
        s.pop();
    }
    s
}

/// Tiny MD5 implementation — avoids pulling in the ``md5`` crate just
/// for ETag hashing. RFC 1321 reference implementation; constant-time
/// is NOT needed (ETags are not secrets).
fn md5_hex_str(data: &[u8]) -> String {
    let digest = md5_digest(data);
    let mut out = String::with_capacity(32);
    for b in digest.iter() {
        use std::fmt::Write;
        let _ = write!(out, "{b:02x}");
    }
    out
}

fn md5_digest(data: &[u8]) -> [u8; 16] {
    // Standard MD5 implementation (RFC 1321).
    const S: [u32; 64] = [
        7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9, 14, 20, 5, 9, 14, 20, 5,
        9, 14, 20, 5, 9, 14, 20, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 6, 10,
        15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
    ];
    const K: [u32; 64] = [
        0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613,
        0xfd469501, 0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193,
        0xa679438e, 0x49b40821, 0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d,
        0x02441453, 0xd8a1e681, 0xe7d3fbc8, 0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed,
        0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a, 0xfffa3942, 0x8771f681, 0x6d9d6122,
        0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70, 0x289b7ec6, 0xeaa127fa,
        0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665, 0xf4292244,
        0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
        0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb,
        0xeb86d391,
    ];

    let mut a0: u32 = 0x67452301;
    let mut b0: u32 = 0xefcdab89;
    let mut c0: u32 = 0x98badcfe;
    let mut d0: u32 = 0x10325476;

    // Pad: append 0x80, then zero bytes, then length in bits as u64 LE.
    let bit_len = (data.len() as u64).wrapping_mul(8);
    let mut padded: Vec<u8> = Vec::with_capacity(data.len() + 72);
    padded.extend_from_slice(data);
    padded.push(0x80);
    while padded.len() % 64 != 56 {
        padded.push(0);
    }
    padded.extend_from_slice(&bit_len.to_le_bytes());

    for chunk in padded.chunks_exact(64) {
        let mut m = [0u32; 16];
        for i in 0..16 {
            m[i] = u32::from_le_bytes([
                chunk[i * 4],
                chunk[i * 4 + 1],
                chunk[i * 4 + 2],
                chunk[i * 4 + 3],
            ]);
        }
        let mut a = a0;
        let mut b = b0;
        let mut c = c0;
        let mut d = d0;
        for i in 0..64 {
            let (f, g) = if i < 16 {
                ((b & c) | (!b & d), i)
            } else if i < 32 {
                ((d & b) | (!d & c), (5 * i + 1) % 16)
            } else if i < 48 {
                (b ^ c ^ d, (3 * i + 5) % 16)
            } else {
                (c ^ (b | !d), (7 * i) % 16)
            };
            let tmp = d;
            d = c;
            c = b;
            b = b.wrapping_add(
                a.wrapping_add(f)
                    .wrapping_add(K[i])
                    .wrapping_add(m[g])
                    .rotate_left(S[i]),
            );
            a = tmp;
        }
        a0 = a0.wrapping_add(a);
        b0 = b0.wrapping_add(b);
        c0 = c0.wrapping_add(c);
        d0 = d0.wrapping_add(d);
    }
    let mut out = [0u8; 16];
    out[..4].copy_from_slice(&a0.to_le_bytes());
    out[4..8].copy_from_slice(&b0.to_le_bytes());
    out[8..12].copy_from_slice(&c0.to_le_bytes());
    out[12..16].copy_from_slice(&d0.to_le_bytes());
    out
}

/// RFC 1123 / ``HTTP-date`` format — ``"Sun, 06 Nov 1994 08:49:37 GMT"``.
/// Uses only Unix seconds so there are no locale / TZ dependencies.
fn format_http_date(secs: u64) -> String {
    // Days since 1970-01-01 (Thursday).
    const SECS_PER_DAY: u64 = 86_400;
    let days = secs / SECS_PER_DAY;
    let rem = secs % SECS_PER_DAY;
    let hour = (rem / 3600) as u32;
    let minute = ((rem % 3600) / 60) as u32;
    let second = (rem % 60) as u32;

    // Day-of-week: 1970-01-01 was a Thursday (index 4).
    let dow_idx = ((days + 4) % 7) as usize;
    let dow = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][dow_idx];

    let (year, month, day) = days_to_ymd(days as i64);
    let month_name = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ][(month - 1) as usize];

    format!("{dow}, {day:02} {month_name} {year:04} {hour:02}:{minute:02}:{second:02} GMT")
}

/// Civil date from days-since-epoch. Howard Hinnant's algorithm
/// (``chrono::days_from_civil`` inverse). Returns (year, month, day).
fn days_to_ymd(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 {
        z / 146_097
    } else {
        (z - 146_096) / 146_097
    };
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

/// Generate a 26-hex-char boundary string for a `multipart/byteranges`
/// response. We combine process-start-time nanos (high entropy) with a
/// per-call atomic counter so back-to-back responses never collide.
fn make_byteranges_boundary() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let c = COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("{nanos:016x}{c:010x}")
}

/// Variant of [`file_response`] that honours a request `Range:` header.
///
/// `range_header` is the raw header value (e.g. `bytes=0-99`). When present
/// and satisfiable, returns `206 Partial Content` with `Content-Range` and
/// a sliced body. Unsatisfiable ranges return `416`. Absent or unparseable
/// ranges fall back to the full-file `200` response.
pub fn file_response_with_range(
    path_str: &str,
    media_type: Option<String>,
    extra_headers: Vec<(String, String)>,
    range_header: Option<&str>,
    if_range_header: Option<&str>,
) -> Response {
    let path = Path::new(path_str);

    // Stat first — we need total_len for range parsing / Content-Range
    // headers, and this is where ENOENT / permission errors surface
    // early (before we commit to any body).
    let meta = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return (StatusCode::NOT_FOUND, format!("File not found: {path_str}")).into_response();
        }
        Err(e) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("File stat error: {e}"),
            )
                .into_response();
        }
    };
    if !meta.is_file() {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("File at path {path_str} is not a file."),
        )
            .into_response();
    }
    let total_len = meta.len();
    let mut ct = media_type.unwrap_or_else(|| "application/octet-stream".to_string());
    if (ct.starts_with("text/") || ct == "application/javascript" || ct == "application/json")
        && !ct.to_lowercase().contains("charset=")
    {
        ct.push_str("; charset=utf-8");
    }

    // Compute stat headers once — the ETag/Last-Modified pair is
    // stamped on every response path (full, 416, single-range, multi-
    // range). ``If-Range`` gating also needs these values BEFORE we
    // commit to a Range-derived status code.
    let (last_modified, etag) = compute_file_stat_headers(&meta);

    // If-Range gating: if the validator doesn't match our entity's
    // Last-Modified or ETag, behave as if Range wasn't sent (RFC 7233
    // §3.2). The client is acting on a stale validator and must
    // re-download the full resource.
    let effective_range = match if_range_header {
        Some(v) if v != last_modified && v != etag => None,
        _ => range_header,
    };

    // Parse Range (if any). Branches:
    //   * Malformed   → 400 (Starlette MalformedRangeHeader parity)
    //   * Unsatisfiable → 416
    //   * Full        → fall through to no-range path
    //   * Single/Multi → emit partial content
    let ranges: Option<Vec<(u64, u64)>> = match effective_range {
        Some(h) => match parse_byte_ranges(h, total_len) {
            RangeOutcome::Full => None,
            RangeOutcome::Single(s, e) => Some(vec![(s, e)]),
            RangeOutcome::Multi(rs) => Some(rs),
            RangeOutcome::Unsatisfiable => {
                // 416 header shape mirrors Starlette 1.0 exactly:
                // Content-Range, Content-Length: 0, Content-Type:
                // text/plain; charset=utf-8. No file stat headers
                // (Last-Modified / ETag) — those are entity-validators
                // and Starlette omits them for the error response.
                // No accept-ranges either.
                return Response::builder()
                    .status(StatusCode::RANGE_NOT_SATISFIABLE)
                    .header("content-range", format!("bytes */{total_len}"))
                    .header("content-length", "0")
                    .header("content-type", "text/plain; charset=utf-8")
                    .body(Body::empty())
                    .expect("build 416");
            }
            RangeOutcome::Malformed(reason) => {
                // 400 mirrors Starlette: Content-Type + Content-Length
                // only. No Last-Modified / ETag / accept-ranges.
                return Response::builder()
                    .status(StatusCode::BAD_REQUEST)
                    .header("content-type", "text/plain; charset=utf-8")
                    .header("content-length", reason.len().to_string())
                    .body(Body::from(reason))
                    .expect("build 400");
            }
        },
        None => None,
    };

    // Multi-range → multipart/byteranges (matches Starlette).
    //
    // Stream the response: a producer task opens the file once, seeks
    // + reads each slice in 64 KiB chunks, and pushes
    // preamble/body/closing frames into an mpsc channel. The response
    // body consumes from the receiver, so peak memory is one chunk
    // per concurrent request — not the full capped ``2 × total_len``
    // that the previous ``Vec<u8>``-accumulation approach used.
    //
    // Content-Length is still computed upfront (sum of preamble
    // lengths + slice lengths + closing length) so clients can
    // progress-bar accurately without chunked framing.
    if let Some(rs) = &ranges {
        if rs.len() > 1 {
            let boundary = make_byteranges_boundary();

            // Precompute every preamble + closing + total body length.
            // Wire format mirrors Starlette 1.0 byte-for-byte: CRLF
            // separators, no leading CRLF on the first preamble,
            // ``\r\n`` between body and next part, ``\r\n--…--``
            // closing (no trailing CRLF). ``ct`` already carries
            // ``; charset=utf-8`` for textual types thanks to the
            // augmentation block above, so the per-part Content-Type
            // matches what Starlette emits for the same file.
            let mut preambles: Vec<bytes::Bytes> = Vec::with_capacity(rs.len());
            let mut body_len: u64 = 0;
            for (idx, (start, end)) in rs.iter().enumerate() {
                let sep = if idx == 0 { "" } else { "\r\n" };
                let pre = format!(
                    "{sep}--{boundary}\r\nContent-Type: {ct}\r\nContent-Range: bytes {start}-{end}/{total_len}\r\n\r\n"
                );
                body_len += pre.len() as u64 + (end - start + 1);
                preambles.push(bytes::Bytes::from(pre));
            }
            let closing = bytes::Bytes::from(format!("\r\n--{boundary}--"));
            body_len += closing.len() as u64;

            // Spawn producer. The axum handler context already runs
            // inside the tokio runtime, so ``tokio::spawn`` picks up
            // the ambient handle.
            let path_buf = path.to_path_buf();
            let ranges_owned: Vec<(u64, u64)> = rs.clone();
            let (tx, rx) = tokio::sync::mpsc::channel::<std::io::Result<bytes::Bytes>>(4);
            tokio::spawn(async move {
                use tokio::io::{AsyncReadExt, AsyncSeekExt};
                let mut fh = match tokio::fs::File::open(&path_buf).await {
                    Ok(f) => f,
                    Err(e) => {
                        let _ = tx.send(Err(e)).await;
                        return;
                    }
                };
                const CHUNK: usize = 64 * 1024;
                let mut buf = vec![0u8; CHUNK];
                for ((start, end), pre) in ranges_owned.into_iter().zip(preambles.into_iter()) {
                    if tx.send(Ok(pre)).await.is_err() {
                        return;
                    }
                    if let Err(e) = fh.seek(std::io::SeekFrom::Start(start)).await {
                        let _ = tx.send(Err(e)).await;
                        return;
                    }
                    let mut remaining: u64 = end - start + 1;
                    while remaining > 0 {
                        let want = (remaining as usize).min(CHUNK);
                        let slice = &mut buf[..want];
                        if let Err(e) = fh.read_exact(slice).await {
                            let _ = tx.send(Err(e)).await;
                            return;
                        }
                        remaining -= want as u64;
                        if tx
                            .send(Ok(bytes::Bytes::copy_from_slice(slice)))
                            .await
                            .is_err()
                        {
                            return;
                        }
                    }
                }
                let _ = tx.send(Ok(closing)).await;
            });

            let stream = tokio_stream::wrappers::ReceiverStream::new(rx);
            let body = Body::from_stream(stream);
            let multipart_ct = format!("multipart/byteranges; boundary={boundary}");
            let mut resp = Response::builder()
                .status(StatusCode::PARTIAL_CONTENT)
                .header("content-type", multipart_ct)
                .header("content-length", body_len)
                .header("accept-ranges", "bytes")
                .header("last-modified", &last_modified)
                .header("etag", &etag)
                .body(body)
                .expect("build multi-range response");
            for (k, v) in extra_headers {
                let k_lower = k.to_ascii_lowercase();
                if matches!(
                    k_lower.as_str(),
                    "content-type"
                        | "content-length"
                        | "accept-ranges"
                        | "content-range"
                        | "last-modified"
                        | "etag"
                ) {
                    continue;
                }
                if let (Ok(hn), Ok(hv)) =
                    (HeaderName::try_from(k.as_str()), HeaderValue::from_str(&v))
                {
                    if k_lower == "set-cookie" {
                        resp.headers_mut().append(hn, hv);
                    } else {
                        resp.headers_mut().insert(hn, hv);
                    }
                }
            }
            return resp;
        }
    }

    // Single-range OR no-range: open the file and stream (or buffer
    // if small). Threshold mirrors ``file_response`` — ≤ 256 KiB uses
    // the fast buffered path; larger reads seek + stream via
    // ``ReaderStream`` so memory is one chunk per concurrent request.
    let (status, content_range, start_off, slice_len) = if let Some(rs) = ranges {
        let (start, end) = rs[0];
        (
            StatusCode::PARTIAL_CONTENT,
            Some(format!("bytes {start}-{end}/{total_len}")),
            start,
            end - start + 1,
        )
    } else {
        (StatusCode::OK, None, 0u64, total_len)
    };

    const STREAM_THRESHOLD: u64 = 256 * 1024;
    let body = if slice_len <= STREAM_THRESHOLD {
        // Small slice — read only the requested bytes into memory.
        // Previously this branch did ``std::fs::read(path)`` which
        // loaded the full file before slicing: a 1-byte range from
        // a 10 GB file allocated 10 GB. The 256 KiB threshold is
        // about OUTPUT body size (buffered vs streamed), not about
        // how many source bytes we can read — source reads must
        // always be bounded by the requested slice.
        use std::io::{Read, Seek, SeekFrom};
        match std::fs::File::open(path) {
            Ok(mut fh) => {
                if start_off > 0 {
                    if let Err(e) = fh.seek(SeekFrom::Start(start_off)) {
                        return (
                            StatusCode::INTERNAL_SERVER_ERROR,
                            format!("File seek error: {e}"),
                        )
                            .into_response();
                    }
                }
                let mut buf = vec![0u8; slice_len as usize];
                if let Err(e) = fh.read_exact(&mut buf) {
                    return (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        format!("File read error: {e}"),
                    )
                        .into_response();
                }
                Body::from(buf)
            }
            Err(e) => {
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("File open error: {e}"),
                )
                    .into_response();
            }
        }
    } else {
        // Large: seek + take + stream. ``tokio::fs::File::from_std``
        // is sync; we build the async stream without needing an
        // async context.
        match std::fs::File::open(path) {
            Ok(std_file) => {
                let tokio_file = tokio::fs::File::from_std(std_file);
                // Seek runs synchronously via the underlying fd —
                // ``set_len`` isn't appropriate here; we use an
                // async-seeking wrapper that reads from ``start_off``.
                use std::io::Seek;
                // Pull the std handle back out to seek sync, then
                // re-wrap for streaming the slice.
                let std_handle = tokio_file
                    .try_into_std()
                    .unwrap_or_else(|_| std::fs::File::open(path).expect("reopen"));
                let mut std_handle = std_handle;
                if start_off > 0 {
                    if let Err(e) = std_handle.seek(std::io::SeekFrom::Start(start_off)) {
                        return (
                            StatusCode::INTERNAL_SERVER_ERROR,
                            format!("File seek error: {e}"),
                        )
                            .into_response();
                    }
                }
                let tokio_file = tokio::fs::File::from_std(std_handle);
                let limited = tokio::io::AsyncReadExt::take(tokio_file, slice_len);
                let stream = tokio_util::io::ReaderStream::new(limited);
                Body::from_stream(stream)
            }
            Err(e) => {
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("File open error: {e}"),
                )
                    .into_response();
            }
        }
    };

    let mut builder = Response::builder()
        .status(status)
        .header("content-type", &ct)
        .header("content-length", slice_len)
        .header("accept-ranges", "bytes")
        .header("last-modified", &last_modified)
        .header("etag", &etag);
    if let Some(ref cr) = content_range {
        builder = builder.header("content-range", cr);
    }
    let mut resp = builder.body(body).expect("build file response");

    for (k, v) in extra_headers {
        let k_lower = k.to_ascii_lowercase();
        if matches!(
            k_lower.as_str(),
            "content-type"
                | "content-length"
                | "accept-ranges"
                | "content-range"
                | "last-modified"
                | "etag"
        ) {
            continue;
        }
        if let (Ok(hn), Ok(hv)) = (HeaderName::try_from(k.as_str()), HeaderValue::from_str(&v)) {
            if k_lower == "set-cookie" {
                resp.headers_mut().append(hn, hv);
            } else {
                resp.headers_mut().insert(hn, hv);
            }
        }
    }
    resp
}
