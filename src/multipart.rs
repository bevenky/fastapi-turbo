//! multipart/form-data parsing for file uploads.
//!
//! Two optimizations vs the previous multer-based implementation:
//!   1. A sync `&[u8]` parser for in-memory bodies — skips multer's async
//!      state machine (~20-30 μs saved per small upload).
//!   2. `PyUploadFile.read()` / `seek()` / `close()` return native Python
//!      awaitables backed by a single `StopIteration`, so `await file.read()`
//!      skips the Python `async def` wrapper entirely (~10-20 μs saved).
//!
//! The file contents are kept in memory (axum's body is already buffered).
//! A spool-to-disk variant could be added later for huge uploads.

use pyo3::exceptions::PyStopIteration;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::collections::HashMap;
use std::sync::Mutex;

/// A single parsed multipart field — either a file (has filename) or a
/// plain form field (just name + value bytes). `data` is a zero-copy slice
/// into the original request body (`bytes::Bytes` is cheap to clone).
pub struct ParsedField {
    pub name: String,
    pub filename: Option<String>,
    pub content_type: Option<String>,
    pub data: bytes::Bytes,
    pub headers: Vec<(String, String)>,
}

// ── Sync multipart parser ──────────────────────────────────────────────
//
// The multipart format is simple enough to parse in-place without async
// machinery when the full body is already in memory:
//
//   --boundary\r\n
//   Content-Disposition: form-data; name="field"[; filename="..."]\r\n
//   [Content-Type: ...\r\n]
//   \r\n
//   <data>\r\n
//   --boundary\r\n
//   ...
//   --boundary--\r\n
//
// RFC 7578 allows trailing whitespace after `--boundary`, which we tolerate.

pub fn parse_multipart_sync(
    body: &bytes::Bytes,
    boundary: &str,
) -> Result<HashMap<String, Vec<ParsedField>>, String> {
    let dash_boundary = format!("--{boundary}");
    let db = dash_boundary.as_bytes();

    let mut result: HashMap<String, Vec<ParsedField>> = HashMap::new();

    // Find first boundary occurrence
    let mut pos = find_bytes(body, db, 0).ok_or_else(|| "boundary not found".to_string())?;

    loop {
        // After `--boundary` we expect either `--` (end) or `\r\n` (more fields).
        let after_b = pos + db.len();
        if after_b + 2 > body.len() {
            return Err("truncated multipart body".to_string());
        }
        if &body[after_b..after_b + 2] == b"--" {
            // End marker `--boundary--`. We're done.
            return Ok(result);
        }
        // Skip optional LWSP + CRLF after boundary
        let mut p = after_b;
        while p < body.len() && (body[p] == b' ' || body[p] == b'\t') {
            p += 1;
        }
        if p + 2 > body.len() || &body[p..p + 2] != b"\r\n" {
            return Err("expected CRLF after boundary".to_string());
        }
        p += 2;

        // Parse headers until blank line
        let header_end = find_bytes(body, b"\r\n\r\n", p)
            .ok_or_else(|| "missing header terminator".to_string())?;
        let header_block = &body[p..header_end];
        let body_start = header_end + 4;

        let mut name = String::new();
        let mut filename: Option<String> = None;
        let mut content_type: Option<String> = None;
        let mut headers: Vec<(String, String)> = Vec::new();

        for line in split_crlf(header_block) {
            let line_s = std::str::from_utf8(line).unwrap_or("");
            if let Some(colon) = line_s.find(':') {
                let hname = line_s[..colon].trim().to_string();
                let hval = line_s[colon + 1..].trim().to_string();
                let lower = hname.to_ascii_lowercase();
                if lower == "content-disposition" {
                    // Parse `form-data; name="x"; filename="y"`
                    name = extract_quoted_param(&hval, "name").unwrap_or_default();
                    filename = extract_quoted_param(&hval, "filename");
                } else if lower == "content-type" {
                    content_type = Some(hval.clone());
                }
                headers.push((hname, hval));
            }
        }

        // Find the next boundary — field data runs up to `\r\n--boundary`
        let terminator = {
            let mut buf = Vec::with_capacity(db.len() + 2);
            buf.extend_from_slice(b"\r\n");
            buf.extend_from_slice(db);
            buf
        };
        let data_end = find_bytes(body, &terminator, body_start)
            .ok_or_else(|| "missing closing boundary for field".to_string())?;
        // Zero-copy slice into the original body — `Bytes::slice` just
        // bumps a refcount on the Arc backing the original buffer.
        let data = body.slice(body_start..data_end);

        result.entry(name.clone()).or_default().push(ParsedField {
            name,
            filename,
            content_type,
            data,
            headers,
        });

        pos = data_end + 2; // position of the next `--boundary`
    }
}

/// Parse the full multipart body — kept as async signature for call-site
/// compatibility, but just defers to the sync parser.
pub async fn parse_multipart(
    body: bytes::Bytes,
    boundary: &str,
) -> Result<HashMap<String, Vec<ParsedField>>, String> {
    parse_multipart_sync(&body, boundary)
}

fn find_bytes(hay: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if needle.is_empty() || from >= hay.len() {
        return None;
    }
    // SIMD-accelerated multi-byte search — ~15 GB/s vs ~1 GB/s for
    // `windows().position()`. For 1 MB multipart bodies this cuts the
    // boundary scan from ~1 ms to ~65 μs.
    memchr::memmem::find(&hay[from..], needle).map(|p| from + p)
}

fn split_crlf(buf: &[u8]) -> impl Iterator<Item = &[u8]> {
    let mut start = 0;
    let mut out = Vec::new();
    let mut i = 0;
    while i + 1 < buf.len() {
        if buf[i] == b'\r' && buf[i + 1] == b'\n' {
            out.push(&buf[start..i]);
            start = i + 2;
            i += 2;
        } else {
            i += 1;
        }
    }
    if start < buf.len() {
        out.push(&buf[start..]);
    }
    out.into_iter()
}

fn extract_quoted_param(hval: &str, key: &str) -> Option<String> {
    // Look for `key="value"` (quoted) or `key=value` (bare) in a header value.
    let needle = format!("{key}=");
    let idx = hval.to_ascii_lowercase().find(&needle)?;
    let rest = &hval[idx + needle.len()..];
    if let Some(rest) = rest.strip_prefix('"') {
        let end = rest.find('"')?;
        Some(rest[..end].to_string())
    } else {
        let end = rest.find(';').unwrap_or(rest.len());
        Some(rest[..end].trim().to_string())
    }
}

// ── ImmediateAwaitable — resolves on the first `__next__` call ─────────
//
// A minimal `await x` primitive that yields a single StopIteration with the
// stored value. This lets PyUploadFile.read() return something the user can
// `await`, without going through Python's `async def` coroutine machinery.

#[pyclass]
pub struct ImmediateBytes {
    value: Option<Py<PyBytes>>,
}

#[pymethods]
impl ImmediateBytes {
    fn __await__(slf: Py<Self>) -> Py<Self> {
        slf
    }
    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }
    fn __next__(mut slf: PyRefMut<'_, Self>) -> PyResult<()> {
        let v = slf.value.take();
        Err(PyStopIteration::new_err(v))
    }
}

#[pyclass]
pub struct ImmediateNone;

#[pymethods]
impl ImmediateNone {
    fn __await__(slf: Py<Self>) -> Py<Self> {
        slf
    }
    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }
    fn __next__(_slf: PyRefMut<'_, Self>) -> PyResult<()> {
        Err(PyStopIteration::new_err(Python::attach(|py| py.None())))
    }
}

/// Python-exposed UploadFile — wraps the parsed bytes with a file-like API
/// matching Starlette's `UploadFile` exactly (async read/seek/close).
///
/// `data` is a zero-copy `bytes::Bytes` slice into the original request body.
#[pyclass]
pub struct PyUploadFile {
    #[pyo3(get)]
    filename: Option<String>,
    #[pyo3(get)]
    content_type: Option<String>,
    #[pyo3(get)]
    size: usize,
    data: bytes::Bytes,
    /// Read cursor — advances on read(), reset by seek().
    cursor: Mutex<usize>,
    /// Headers from the multipart part.
    header_list: Vec<(String, String)>,
    /// Starlette-parity: ``file.closed`` flips to ``True`` after ``close()``.
    closed: Mutex<bool>,
}

#[pymethods]
impl PyUploadFile {
    /// Async read: returns an awaitable that resolves to bytes. Matches
    /// Starlette's UploadFile.read signature. Because we have the data in
    /// memory, the awaitable resolves immediately (no scheduling hop).
    #[pyo3(signature = (size=-1))]
    fn read<'py>(&self, py: Python<'py>, size: isize) -> PyResult<Py<ImmediateBytes>> {
        let mut cursor = self.cursor.lock().unwrap();
        let remaining = self.data.len().saturating_sub(*cursor);
        let take = if size < 0 {
            remaining
        } else {
            std::cmp::min(remaining, size as usize)
        };
        let slice = &self.data[*cursor..*cursor + take];
        *cursor += take;
        let py_bytes = PyBytes::new(py, slice).unbind();
        Py::new(
            py,
            ImmediateBytes {
                value: Some(py_bytes),
            },
        )
    }

    /// Async seek.
    fn seek(&self, py: Python<'_>, offset: isize) -> PyResult<Py<ImmediateNone>> {
        let mut cursor = self.cursor.lock().unwrap();
        let clamped = if offset < 0 {
            0
        } else {
            std::cmp::min(offset as usize, self.data.len())
        };
        *cursor = clamped;
        Py::new(py, ImmediateNone)
    }

    fn tell(&self) -> PyResult<usize> {
        Ok(*self.cursor.lock().unwrap())
    }

    /// Async close — no-op for in-memory uploads, but flips ``closed``.
    fn close(&self, py: Python<'_>) -> PyResult<Py<ImmediateNone>> {
        *self.closed.lock().unwrap() = true;
        Py::new(py, ImmediateNone)
    }

    #[getter]
    fn closed(&self) -> bool {
        *self.closed.lock().unwrap()
    }

    /// Async write — not really writable, but matches signature.
    fn write<'py>(&self, py: Python<'py>, _data: Bound<'py, PyAny>) -> PyResult<Py<ImmediateNone>> {
        Py::new(py, ImmediateNone)
    }

    /// Starlette-compat: UploadFile.file exposes a SpooledTemporaryFile-like.
    /// We return self — our own .read/.seek methods cover the important calls.
    #[getter]
    fn file<'py>(slf: PyRef<'py, Self>) -> PyRef<'py, Self> {
        slf
    }

    #[getter]
    fn headers<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        for (k, v) in &self.header_list {
            d.set_item(k, v)?;
        }
        Ok(d)
    }
}

impl PyUploadFile {
    pub fn from_field(field: ParsedField) -> Self {
        let size = field.data.len();
        Self {
            filename: field.filename,
            content_type: field.content_type,
            size,
            data: field.data,
            cursor: Mutex::new(0),
            header_list: field.headers,
            closed: Mutex::new(false),
        }
    }
}

/// Extract the multipart boundary from a Content-Type header value.
///
/// Format: `multipart/form-data; boundary=XYZ` (possibly quoted).
pub fn parse_boundary(content_type: &str) -> Option<String> {
    let lower = content_type.to_ascii_lowercase();
    if !lower.trim_start().starts_with("multipart/") {
        return None;
    }
    extract_quoted_param(content_type, "boundary")
}
