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

use pyo3::exceptions::{PyStopIteration, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

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

/// Sync file-like exposing the same shared buffer as ``PyUploadFile``.
/// Returned from ``PyUploadFile.file`` so callers that hand the file
/// to a sync consumer (``shutil.copyfileobj(upload.file, dest)``,
/// ``image_lib.open(upload.file)``) get the bytes/int return shapes
/// they expect — Starlette backs ``.file`` with ``SpooledTemporaryFile``,
/// whose methods return raw values, not awaitables. Earlier the
/// getter returned ``self``, so ``upload.file.read()`` produced an
/// awaitable wrapper instead of bytes and broke any sync consumer.
#[pyclass]
pub struct PySyncFile {
    data: Arc<Mutex<Vec<u8>>>,
    cursor: Arc<Mutex<usize>>,
    closed: Arc<Mutex<bool>>,
}

#[pymethods]
impl PySyncFile {
    /// Sync read returning bytes. ``size = -1`` reads to EOF.
    #[pyo3(signature = (size=-1))]
    fn read<'py>(&self, py: Python<'py>, size: isize) -> PyResult<Py<PyBytes>> {
        let data = self.data.lock().unwrap();
        let mut cursor = self.cursor.lock().unwrap();
        let remaining = data.len().saturating_sub(*cursor);
        let take = if size < 0 {
            remaining
        } else {
            std::cmp::min(remaining, size as usize)
        };
        let slice = &data[*cursor..*cursor + take];
        let py_bytes = PyBytes::new(py, slice).unbind();
        *cursor += take;
        Ok(py_bytes)
    }

    /// Sync write returning the integer byte count (matches
    /// ``io.BufferedIOBase.write`` / ``SpooledTemporaryFile.write``).
    fn write<'py>(&self, _py: Python<'py>, data: Bound<'py, PyAny>) -> PyResult<usize> {
        let buf: Vec<u8> = if let Ok(b) = data.cast::<PyBytes>() {
            b.as_bytes().to_vec()
        } else {
            data.extract::<Vec<u8>>().map_err(|e| {
                PyValueError::new_err(format!(
                    "UploadFile.file.write expects bytes-like, got error: {e}"
                ))
            })?
        };
        let mut storage = self.data.lock().unwrap();
        let mut cursor = self.cursor.lock().unwrap();
        let pos = *cursor;
        let new_end = pos + buf.len();
        if new_end > storage.len() {
            storage.resize(new_end, 0);
        }
        storage[pos..new_end].copy_from_slice(&buf);
        *cursor = new_end;
        Ok(buf.len())
    }

    /// Sync seek; returns the new cursor position to mirror
    /// ``io.IOBase.seek`` (which returns the absolute position).
    #[pyo3(signature = (offset, whence=0))]
    fn seek(&self, offset: isize, whence: i32) -> PyResult<usize> {
        let data_len = self.data.lock().unwrap().len();
        let mut cursor = self.cursor.lock().unwrap();
        let target: isize = match whence {
            0 => offset,
            1 => *cursor as isize + offset,
            2 => data_len as isize + offset,
            _ => return Err(PyValueError::new_err("invalid whence")),
        };
        let clamped = if target < 0 {
            0
        } else {
            std::cmp::min(target as usize, data_len)
        };
        *cursor = clamped;
        Ok(clamped)
    }

    fn tell(&self) -> PyResult<usize> {
        Ok(*self.cursor.lock().unwrap())
    }

    fn close(&self) -> PyResult<()> {
        *self.closed.lock().unwrap() = true;
        Ok(())
    }

    #[getter]
    fn closed(&self) -> bool {
        *self.closed.lock().unwrap()
    }
}

/// Python-exposed UploadFile — wraps the parsed bytes with a file-like API
/// matching Starlette's `UploadFile` exactly (async read/seek/write/close).
///
/// `data` starts as a copy of the multipart slice and is mutable so
/// ``write()`` can extend or overwrite at the cursor (like Starlette's
/// `SpooledTemporaryFile`-backed `UploadFile.file`). The original
/// `bytes::Bytes` is unchanged — handlers that mutate the upload don't
/// affect the request body.
#[pyclass]
pub struct PyUploadFile {
    #[pyo3(get)]
    filename: Option<String>,
    #[pyo3(get)]
    content_type: Option<String>,
    /// Mutable buffer — supports both reads (cursor-based slicing) and
    /// writes (cursor-positioned overwrite + extend). Held behind an
    /// ``Arc<Mutex>`` so the sync ``.file`` view (``PySyncFile``) can
    /// share the same backing buffer — Starlette's ``UploadFile`` and
    /// ``UploadFile.file`` mutate the same bytes.
    data: Arc<Mutex<Vec<u8>>>,
    /// Read / write cursor — advances on read() and write(), reset by
    /// seek(). Shared with ``PySyncFile`` for the same reason as
    /// ``data``: a sync seek on ``.file`` must move the async cursor too.
    cursor: Arc<Mutex<usize>>,
    /// Logical size — separate from ``data.len()``. Async
    /// ``await upload.write(b)`` increments this by ``len(b)``
    /// regardless of cursor position (matches Starlette's
    /// ``UploadFile.write`` semantics: ``self.size += len(data)``).
    /// Sync ``upload.file.write(b)`` mutates ``data`` / ``cursor`` but
    /// does NOT touch ``size`` — Starlette's underlying
    /// ``SpooledTemporaryFile.write`` is opaque to ``UploadFile.size``.
    /// Initialised to the parsed multipart slice length.
    size: Arc<Mutex<usize>>,
    /// Headers from the multipart part.
    header_list: Vec<(String, String)>,
    /// Starlette-parity: ``file.closed`` flips to ``True`` after ``close()``.
    closed: Arc<Mutex<bool>>,
}

#[pymethods]
impl PyUploadFile {
    /// Logical size, tracked separately from the backing buffer.
    /// Async ``await upload.write(b)`` increments this; sync
    /// ``upload.file.write(b)`` does not — same divergence Starlette
    /// has between ``UploadFile.size`` and the underlying
    /// ``SpooledTemporaryFile``. Initialised to the parsed multipart
    /// slice length (so a freshly-uploaded file reports its on-the-wire
    /// size before any writes happen).
    #[getter]
    fn size(&self) -> usize {
        *self.size.lock().unwrap()
    }

    /// Async read: returns an awaitable that resolves to bytes. Matches
    /// Starlette's UploadFile.read signature. Because we have the data in
    /// memory, the awaitable resolves immediately (no scheduling hop).
    #[pyo3(signature = (size=-1))]
    fn read<'py>(&self, py: Python<'py>, size: isize) -> PyResult<Py<ImmediateBytes>> {
        let data = self.data.lock().unwrap();
        let mut cursor = self.cursor.lock().unwrap();
        let remaining = data.len().saturating_sub(*cursor);
        let take = if size < 0 {
            remaining
        } else {
            std::cmp::min(remaining, size as usize)
        };
        let slice = &data[*cursor..*cursor + take];
        let py_bytes = PyBytes::new(py, slice).unbind();
        *cursor += take;
        Py::new(
            py,
            ImmediateBytes {
                value: Some(py_bytes),
            },
        )
    }

    /// Async seek.
    fn seek(&self, py: Python<'_>, offset: isize) -> PyResult<Py<ImmediateNone>> {
        let data_len = self.data.lock().unwrap().len();
        let mut cursor = self.cursor.lock().unwrap();
        let clamped = if offset < 0 {
            0
        } else {
            std::cmp::min(offset as usize, data_len)
        };
        *cursor = clamped;
        Py::new(py, ImmediateNone)
    }

    fn tell(&self) -> PyResult<usize> {
        Ok(*self.cursor.lock().unwrap())
    }

    /// Async close — flips ``closed``. Buffer is retained so subsequent
    /// reads after close return what was written (matches Starlette's
    /// SpooledTemporaryFile behaviour).
    fn close(&self, py: Python<'_>) -> PyResult<Py<ImmediateNone>> {
        *self.closed.lock().unwrap() = true;
        Py::new(py, ImmediateNone)
    }

    #[getter]
    fn closed(&self) -> bool {
        *self.closed.lock().unwrap()
    }

    /// Async write — extends or overwrites the buffer at the cursor.
    /// Matches Starlette's ``UploadFile.write(b)``: bytes-like input,
    /// extends past EOF, advances cursor. Returns ``None`` (the
    /// awaitable resolves to ``None``) to match Starlette's signature
    /// — callers that need the byte count should use the sync
    /// ``upload.file.write(b)`` path which returns an int per the
    /// ``io.IOBase`` contract.
    ///
    /// Earlier impl was a no-op that ignored its argument. The R19
    /// fix added real write semantics but returned an int, which
    /// diverged from Starlette's ``async def write(...) -> None``.
    fn write<'py>(&self, py: Python<'py>, data: Bound<'py, PyAny>) -> PyResult<Py<ImmediateNone>> {
        // Accept any bytes-like (bytes, bytearray, memoryview).
        let buf: Vec<u8> = if let Ok(b) = data.cast::<PyBytes>() {
            b.as_bytes().to_vec()
        } else {
            // Fallback: try to extract via the buffer protocol.
            data.extract::<Vec<u8>>().map_err(|e| {
                PyValueError::new_err(format!(
                    "UploadFile.write expects bytes-like, got error: {e}"
                ))
            })?
        };
        let written = buf.len();
        {
            let mut storage = self.data.lock().unwrap();
            let mut cursor = self.cursor.lock().unwrap();
            let pos = *cursor;
            let new_end = pos + written;
            if new_end > storage.len() {
                storage.resize(new_end, 0);
            }
            storage[pos..new_end].copy_from_slice(&buf);
            *cursor = new_end;
        }
        // Starlette's ``UploadFile.write`` does ``self.size += len
        // (data)`` unconditionally — the size accumulates across
        // every async write, even when the write overlays existing
        // bytes (so ``size`` doesn't necessarily equal the buffer
        // length). Mirror that here so probes that compare
        // ``upstream.size`` to ``turbo.size`` agree on the count.
        *self.size.lock().unwrap() += written;
        Py::new(py, ImmediateNone)
    }

    /// Starlette-compat: ``UploadFile.file`` exposes a sync
    /// SpooledTemporaryFile-like object. We hand back a fresh
    /// ``PySyncFile`` that shares the underlying buffer / cursor /
    /// closed flag via ``Arc<Mutex<…>>`` — sync reads/writes/seeks
    /// on ``.file`` are immediately visible to the async API on
    /// ``UploadFile`` and vice-versa. ``.file.read()`` returns
    /// ``bytes`` directly (not an awaitable wrapper) so libraries
    /// that pass ``upload.file`` to a sync consumer
    /// (``shutil.copyfileobj(upload.file, dest)`` etc.) work.
    #[getter]
    fn file(&self, py: Python<'_>) -> PyResult<Py<PySyncFile>> {
        Py::new(
            py,
            PySyncFile {
                data: self.data.clone(),
                cursor: self.cursor.clone(),
                closed: self.closed.clone(),
            },
        )
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
        let initial_len = field.data.len();
        Self {
            filename: field.filename,
            content_type: field.content_type,
            data: Arc::new(Mutex::new(field.data.to_vec())),
            cursor: Arc::new(Mutex::new(0)),
            size: Arc::new(Mutex::new(initial_len)),
            header_list: field.headers,
            closed: Arc::new(Mutex::new(false)),
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
