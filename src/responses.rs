use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyNone, PyString};

/// Convert a Python return value into an Axum `Response`.
/// Checks are ordered by frequency: dict (most common) first, then special types.
pub fn py_to_response(py: Python<'_>, obj: &Bound<'_, PyAny>) -> Response {
    // dict or list -> JSON (MOST COMMON — check first, skip attr lookups)
    // Always use orjson when available (5-7x faster than alternatives at all sizes)
    if obj.is_instance_of::<PyDict>() || obj.is_instance_of::<PyList>() {
        let json_str = if let Ok(orjson) = py.import("orjson") {
            if let Ok(bytes) = orjson.call_method1("dumps", (obj,)) {
                if let Ok(b) = bytes.extract::<Vec<u8>>() {
                    unsafe { String::from_utf8_unchecked(b) }
                } else {
                    fallback_json(py, obj)
                }
            } else {
                fallback_json(py, obj)
            }
        } else {
            fallback_json(py, obj)
        };
        return (
            StatusCode::OK,
            [("content-type", "application/json")],
            json_str,
        )
            .into_response();
    }

    // None -> 204 No Content
    if obj.is_instance_of::<PyNone>() {
        return StatusCode::NO_CONTENT.into_response();
    }

    // Has `body_iterator` attribute -> StreamingResponse
    if obj.hasattr("body_iterator").unwrap_or(false) {
        if let Ok(body_iter) = obj.getattr("body_iterator") {
            if !body_iter.is_none() {
                return crate::streaming::create_streaming_response(py, obj);
            }
        }
    }

    // Has `.status_code` attribute -> treat as a Response-like object
    if let Ok(status_attr) = obj.getattr("status_code") {
        return response_object_to_response(py, obj, &status_attr);
    }

    // str -> plain text
    if let Ok(s) = obj.extract::<String>() {
        return (StatusCode::OK, [("content-type", "text/plain")], s).into_response();
    }

    // int / float -> JSON number
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

    // bool -> JSON boolean
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

    // Fallback: str() it
    let repr = obj.str().map(|s| s.to_string()).unwrap_or_default();
    (StatusCode::OK, [("content-type", "text/plain")], repr).into_response()
}

/// Convert a Python Response-like object (has status_code, headers, body).
fn response_object_to_response(
    py: Python<'_>,
    obj: &Bound<'_, PyAny>,
    status_attr: &Bound<'_, PyAny>,
) -> Response {
    let status_code = status_attr.extract::<u16>().unwrap_or(200);
    let status =
        StatusCode::from_u16(status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

    let mut headers = HeaderMap::new();
    if let Ok(hdr_dict) = obj.getattr("headers") {
        if let Ok(dict) = hdr_dict.downcast::<PyDict>() {
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
    }
    // raw_headers list preserves duplicates (e.g., multiple Set-Cookie).
    // Use append() instead of insert() so multiple values of the same name survive.
    if let Ok(raw_attr) = obj.getattr("raw_headers") {
        if let Ok(list) = raw_attr.downcast::<PyList>() {
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

    let body = if let Ok(b) = obj.getattr("body") {
        if let Ok(s) = b.extract::<String>() {
            s
        } else if let Ok(bytes) = b.extract::<Vec<u8>>() {
            String::from_utf8_lossy(&bytes).to_string()
        } else {
            // Try dict-like body
            let val = pyobj_to_serde(py, &b);
            serde_json::to_string(&val).unwrap_or_default()
        }
    } else {
        String::new()
    };

    (status, headers, body).into_response()
}

/// Convert a PyErr into an HTTP error response.
/// If the error value has `status_code` and `detail`, format like FastAPI's HTTPException.
pub fn pyerr_to_response(py: Python<'_>, err: &PyErr) -> Response {
    let err_value = err.value(py);

    let status_code = err_value
        .getattr("status_code")
        .ok()
        .and_then(|a| a.extract::<u16>().ok())
        .unwrap_or(500);

    let detail = err_value
        .getattr("detail")
        .ok()
        .and_then(|a| a.extract::<String>().ok())
        .unwrap_or_else(|| format!("{err}"));

    let status =
        StatusCode::from_u16(status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

    let body = serde_json::json!({ "detail": detail });

    (
        status,
        [("content-type", "application/json")],
        body.to_string(),
    )
        .into_response()
}

/// Fallback JSON serialization when orjson is not available.
fn fallback_json(py: Python<'_>, obj: &Bound<'_, PyAny>) -> String {
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let mut buf = String::with_capacity(128);
        write_dict_json(py, dict, &mut buf);
        buf
    } else if let Ok(list) = obj.downcast::<PyList>() {
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
        if let Ok(s) = k.downcast::<PyString>() {
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
    if let Ok(b) = obj.downcast::<PyBool>() {
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
    if let Ok(s) = obj.downcast::<PyString>() {
        buf.push('"');
        json_escape_to(s.to_str().unwrap_or(""), buf);
        buf.push('"');
        return;
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        write_dict_json(py, dict, buf);
        return;
    }
    if let Ok(list) = obj.downcast::<PyList>() {
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
    if let Ok(b) = obj.downcast::<PyBool>() {
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
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let mut map = serde_json::Map::new();
        for (k, v) in dict.iter() {
            let key = k.str().map(|s| s.to_string()).unwrap_or_default();
            map.insert(key, pyobj_to_serde(py, &v));
        }
        return serde_json::Value::Object(map);
    }
    if let Ok(list) = obj.downcast::<PyList>() {
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
