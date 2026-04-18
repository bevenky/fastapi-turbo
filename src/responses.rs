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

/// Cached fastapi_rs.responses class pointers. A Python type pointer is
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
    if let Ok(m) = py.import("fastapi_rs.responses") {
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

/// Cached `float` builtin — used as `default=float` for orjson so that
/// `decimal.Decimal` values (returned by psycopg3's numeric columns) get
/// auto-converted to float instead of raising TypeError.
static FLOAT_TYPE: OnceLock<Py<PyAny>> = OnceLock::new();

fn float_type(py: Python<'_>) -> &'static Py<PyAny> {
    FLOAT_TYPE.get_or_init(|| {
        py.import("builtins")
            .expect("builtins")
            .getattr("float")
            .expect("float")
            .unbind()
    })
}

/// Cached kwargs dict for orjson.dumps: `{"default": float}`.
/// Allocated once, reused forever — saves ~1-2μs per JSON response
/// vs allocating a fresh PyDict on every call.
static ORJSON_KWARGS: OnceLock<Py<PyAny>> = OnceLock::new();

fn orjson_kwargs(py: Python<'_>) -> &'static Py<PyAny> {
    ORJSON_KWARGS.get_or_init(|| {
        let d = pyo3::types::PyDict::new(py);
        d.set_item("default", float_type(py).bind(py)).expect("set default");
        d.unbind().into_any()
    })
}

/// Serialize a dict/list to JSON bytes via cached orjson (or stdlib fallback).
/// Uses `default=float` so `decimal.Decimal` values serialize correctly —
/// psycopg3 returns Decimal for PostgreSQL `numeric` columns.
fn dict_to_json_bytes(py: Python<'_>, obj: &Bound<'_, PyAny>) -> Vec<u8> {
    if let Some(dumps) = orjson_dumps(py) {
        let kw = orjson_kwargs(py);
        if let Ok(bytes) = dumps.call(py, (obj,), Some(kw.bind(py).downcast().unwrap())) {
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
    let is_plain_response = JSON_RESPONSE_CLS.get().map_or(false, |c| ty.is(c.bind(py)))
        || PLAIN_RESPONSE_CLS.get().map_or(false, |c| ty.is(c.bind(py)))
        || HTML_RESPONSE_CLS.get().map_or(false, |c| ty.is(c.bind(py)));
    if is_plain_response {
        if let Ok(status_attr) = obj.getattr("status_code") {
            return response_object_to_response(py, obj, &status_attr);
        }
    }

    // StreamingResponse: vLLM / SGLang return a new one on every
    // chat/completions request, so its dispatch sits on the TTFB hot path.
    // Exact-class check avoids the generic getattr("status_code") →
    // getattr("path") → getattr("body_iterator") probe below.
    if STREAMING_RESPONSE_CLS.get().map_or(false, |c| ty.is(c.bind(py))) {
        return crate::streaming::create_streaming_response(py, obj);
    }

    // FileResponse: skip path/media_type/headers getattr-chain if we can
    // confirm the exact class. Fall back to the generic attr-probe path
    // for unknown subclasses.
    if FILE_RESPONSE_CLS.get().map_or(false, |c| ty.is(c.bind(py))) {
        if let Ok(path_attr) = obj.getattr("path") {
            if let Ok(path_str) = path_attr.extract::<String>() {
                let media_type = obj.getattr("media_type")
                    .ok()
                    .and_then(|a| a.extract::<String>().ok());
                let headers = extract_response_headers(obj);
                return file_response(&path_str, media_type, headers);
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
                    let media_type = obj.getattr("media_type")
                        .ok()
                        .and_then(|a| a.extract::<String>().ok());
                    let headers = extract_response_headers(obj);
                    return file_response(&path_str, media_type, headers);
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

    // Pydantic BaseModel: call model_dump() then serialize as JSON.
    // Checked BEFORE primitives because BaseModel instances may also satisfy
    // extract::<String>() via __str__.
    if let Ok(dump) = obj.getattr("model_dump") {
        if dump.is_callable() {
            if let Ok(dumped) = dump.call0() {
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
        return (
            StatusCode::OK,
            [("content-type", "application/json")],
            body,
        )
            .into_response();
    }

    // int / float -> JSON number (matches FastAPI: all scalars are JSON-serialized)
    if obj.is_instance_of::<PyInt>() || obj.is_instance_of::<PyFloat>() {
        let value = pyobj_to_serde(py, obj);
        let body = serde_json::to_string(&value).unwrap_or_else(|_| "null".to_string());
        return (
            StatusCode::OK,
            [("content-type", "application/json")],
            body,
        )
            .into_response();
    }

    // str -> JSON-wrapped string (matches FastAPI: strings are JSON-serialized)
    if let Ok(s) = obj.extract::<String>() {
        let json = format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""));
        return (StatusCode::OK, [("content-type", "application/json")], json).into_response();
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
    let status =
        StatusCode::from_u16(status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

    let mut headers = HeaderMap::new();
    if let Ok(hdr_obj) = obj.getattr("headers") {
        // Support both plain dict and MutableHeaders (which has .items())
        if let Ok(dict) = hdr_obj.downcast::<PyDict>() {
            if !dict.is_empty() {
                for (k, v) in dict.iter() {
                    if let (Ok(key), Ok(val)) = (k.extract::<String>(), v.extract::<String>()) {
                        if let (Ok(hname), Ok(hval)) = (
                            HeaderName::try_from(key),
                            HeaderValue::from_str(&val),
                        ) {
                            headers.insert(hname, hval);
                        }
                    }
                }
            }
        } else if let Ok(items_list) = hdr_obj.call_method0("items") {
            if let Ok(list) = items_list.downcast::<pyo3::types::PyList>() {
                for item in list.iter() {
                    if let Ok((key, val)) = item.extract::<(String, String)>() {
                        if let (Ok(hname), Ok(hval)) = (
                            HeaderName::try_from(key),
                            HeaderValue::from_str(&val),
                        ) {
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
                        if let (Ok(hname), Ok(hval)) = (
                            HeaderName::try_from(tup.0),
                            HeaderValue::from_str(&tup.1),
                        ) {
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
            bytes::Bytes::from(
                serde_json::to_vec(&val).unwrap_or_default(),
            )
        }
    } else {
        bytes::Bytes::new()
    };

    let mut resp = axum::response::Response::builder()
        .status(status)
        .body(axum::body::Body::from(body_bytes))
        .expect("build response");
    let hmap = resp.headers_mut();
    for (k, v) in headers.iter() {
        hmap.insert(k, v.clone());
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
            let detail = err_value
                .getattr("detail")
                .ok()
                .and_then(|a| a.extract::<String>().ok())
                .unwrap_or_else(|| format!("{err}"));

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

            let body = serde_json::json!({ "detail": detail });
            let mut resp = axum::response::Response::builder()
                .status(status)
                .header("content-type", "application/json")
                .body(axum::body::Body::from(body.to_string()))
                .expect("build response");
            for (k, v) in extra_headers {
                if let (Ok(hn), Ok(hv)) = (
                    HeaderName::try_from(k.as_str()),
                    HeaderValue::from_str(&v),
                ) {
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
        if !first { buf.push(','); }
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
        if i > 0 { buf.push(','); }
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
        let arr: Vec<serde_json::Value> = list.iter().map(|item| pyobj_to_serde(py, &item)).collect();
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
        if let Ok(dict) = hdr.downcast::<PyDict>() {
            for (k, v) in dict.iter() {
                if let (Ok(ks), Ok(vs)) = (k.extract::<String>(), v.extract::<String>()) {
                    out.push((ks, vs));
                }
            }
        } else if let Ok(items_list) = hdr.call_method0("items") {
            if let Ok(list) = items_list.downcast::<PyList>() {
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

    let data = match std::fs::read(path) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return (StatusCode::NOT_FOUND, format!("File not found: {path_str}")).into_response();
        }
        Err(e) => {
            return (StatusCode::INTERNAL_SERVER_ERROR, format!("File read error: {e}"))
                .into_response();
        }
    };

    let total_len = data.len() as u64;
    let mut ct = media_type.unwrap_or_else(|| "application/octet-stream".to_string());
    // FastAPI/Starlette append `; charset=utf-8` to textual types if absent.
    if ct.starts_with("text/") || ct == "application/javascript" || ct == "application/json" {
        if !ct.to_lowercase().contains("charset=") {
            ct.push_str("; charset=utf-8");
        }
    }

    let mut resp = Response::builder()
        .status(StatusCode::OK)
        .header("content-type", &ct)
        .header("content-length", total_len)
        .header("accept-ranges", "bytes")
        .body(Body::from(data))
        .expect("build file response");

    for (k, v) in extra_headers {
        let k_lower = k.to_ascii_lowercase();
        // Skip headers we've already set (avoids duplicate Content-Type/Length)
        if matches!(k_lower.as_str(), "content-type" | "content-length" | "accept-ranges") {
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

// Range header parsing lives in tower-http's ServeDir — we no longer parse
// ranges ourselves since FileResponse delegates full-file serving to
// `file_response()` and range requests fall through to ServeDir.
