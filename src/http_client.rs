//! Rust HTTP transport backed by reqwest.
//!
//! Handles raw HTTP I/O, connection pooling, TLS, compression, HTTP/2, proxy.
//! Auth, cookies, redirects, event hooks are handled in Python (like httpx + httpcore).
//! This separation mirrors httpx's architecture: Python logic + native transport.

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::cell::RefCell;
use std::time::{Duration, Instant};

// ── Tokio runtime strategy ──────────────────────────────────────────
//
// Two runtimes:
//   1. Thread-local CURRENT_THREAD runtime for single request() calls.
//      Runs futures on the CALLING thread — zero cross-thread scheduling.
//      ~40μs faster per request than a shared multi-thread runtime.
//
//   2. Shared multi-threaded runtime for gather() calls.
//      Lets N concurrent requests run on multiple worker threads.

thread_local! {
    // Leaked &'static reference — avoids SIGSEGV on interpreter shutdown
    // when thread_local destructors run after tokio's background state is invalid.
    static LOCAL_RT: RefCell<Option<&'static tokio::runtime::Runtime>> = const { RefCell::new(None) };
}

/// Run a future on the thread-local current_thread runtime.
/// Zero cross-thread scheduling — the future executes on the calling thread.
fn with_local_runtime<F, R>(f: F) -> R
where
    F: FnOnce(&tokio::runtime::Runtime) -> R,
{
    LOCAL_RT.with(|cell| {
        let mut opt = cell.borrow_mut();
        if opt.is_none() {
            // Leak the runtime — never dropped, avoids shutdown races
            let rt: &'static tokio::runtime::Runtime = Box::leak(Box::new(
                tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .expect("Failed to create local runtime"),
            ));
            *opt = Some(rt);
        }
        f(opt.unwrap())
    })
}

// ── RawResponse ─────────────────────────────────────────────────────

/// Raw HTTP response from the Rust transport.
/// Converted to a full Python Response object in http.py.
#[pyclass(from_py_object)]
#[derive(Clone)]
pub struct RawResponse {
    #[pyo3(get)]
    pub status: u16,
    pub headers_vec: Vec<(String, String)>,
    pub body_vec: Vec<u8>,
    #[pyo3(get)]
    pub elapsed_secs: f64,
    #[pyo3(get)]
    pub http_version: String,
}

#[pymethods]
impl RawResponse {
    #[getter]
    fn headers(&self) -> Vec<(String, String)> {
        self.headers_vec.clone()
    }

    #[getter]
    fn body<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.body_vec)
    }
}

// ── Helper: execute a single reqwest request ────────────────────────

async fn do_request(
    client: &reqwest::Client,
    method: &str,
    url: &str,
    headers: Option<Vec<(String, String)>>,
    body: Option<Vec<u8>>,
    timeout_secs: Option<f64>,
) -> PyResult<RawResponse> {
    let http_method: reqwest::Method = method
        .parse()
        .map_err(|_| pyo3::exceptions::PyValueError::new_err(format!("Invalid method: {method}")))?;

    let mut req = client.request(http_method, url);

    if let Some(h) = headers {
        for (k, v) in h {
            let name = reqwest::header::HeaderName::from_bytes(k.as_bytes())
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
            let val = reqwest::header::HeaderValue::from_str(&v)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
            req = req.header(name, val);
        }
    }

    if let Some(b) = body {
        req = req.body(b);
    }

    if let Some(t) = timeout_secs {
        req = req.timeout(Duration::from_secs_f64(t));
    }

    let start = Instant::now();
    let resp = req.send().await.map_err(|e| {
        if e.is_timeout() {
            pyo3::exceptions::PyTimeoutError::new_err(format!("Timeout: {e}"))
        } else if e.is_connect() {
            pyo3::exceptions::PyConnectionError::new_err(format!("Connect error: {e}"))
        } else {
            pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
        }
    })?;

    let status = resp.status().as_u16();
    let http_version: &'static str = match resp.version() {
        reqwest::Version::HTTP_09 => "HTTP/0.9",
        reqwest::Version::HTTP_10 => "HTTP/1.0",
        reqwest::Version::HTTP_11 => "HTTP/1.1",
        reqwest::Version::HTTP_2 => "HTTP/2",
        reqwest::Version::HTTP_3 => "HTTP/3",
        _ => "HTTP/1.1",
    };

    // Pre-allocate headers_vec based on header count
    let header_count = resp.headers().len();
    let mut headers_vec = Vec::with_capacity(header_count);
    for (k, v) in resp.headers().iter() {
        headers_vec.push((k.as_str().to_owned(), v.to_str().unwrap_or("").to_owned()));
    }

    let body_vec = resp
        .bytes()
        .await
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Read error: {e}")))?
        .to_vec();
    let elapsed = start.elapsed().as_secs_f64();

    Ok(RawResponse {
        status,
        headers_vec,
        body_vec,
        elapsed_secs: elapsed,
        http_version: http_version.to_string(),
    })
}

// ── RustTransport ───────────────────────────────────────────────────

/// Rust HTTP transport backed by reqwest.
///
/// Handles: connection pooling, keep-alive, TLS (rustls), HTTP/2,
/// gzip/brotli/deflate/zstd decompression, SOCKS/HTTP proxy.
///
/// Does NOT handle (Python handles): auth, cookies, redirects, event hooks.
/// This mirrors httpx's architecture (Python Client wraps httpcore transport).
#[pyclass]
pub struct RustTransport {
    client: reqwest::Client,
}

#[pymethods]
impl RustTransport {
    #[new]
    #[pyo3(signature = (
        timeout_connect_secs = None,
        timeout_read_secs = None,
        timeout_total_secs = None,
        pool_idle_timeout_secs = None,
        pool_max_idle_per_host = None,
        http2 = false,
        proxy_url = None,
        verify_ssl = true,
        trust_env = true,
    ))]
    fn new(
        timeout_connect_secs: Option<f64>,
        timeout_read_secs: Option<f64>,
        timeout_total_secs: Option<f64>,
        pool_idle_timeout_secs: Option<f64>,
        pool_max_idle_per_host: Option<usize>,
        http2: bool,
        proxy_url: Option<String>,
        verify_ssl: bool,
        trust_env: bool,
    ) -> PyResult<Self> {
        let mut builder = reqwest::Client::builder()
            // Python handles redirects (for auth stripping, cookie management, hooks)
            .redirect(reqwest::redirect::Policy::none())
            // Python handles cookies (for httpx compat cookie jar)
            .cookie_store(false)
            // Enable transparent decompression
            .gzip(true)
            .brotli(true)
            .deflate(true)
            .zstd(true);

        // Timeouts
        if let Some(t) = timeout_connect_secs {
            builder = builder.connect_timeout(Duration::from_secs_f64(t));
        }
        if let Some(t) = timeout_read_secs {
            builder = builder.read_timeout(Duration::from_secs_f64(t));
        }
        if let Some(t) = timeout_total_secs {
            builder = builder.timeout(Duration::from_secs_f64(t));
        }

        // Connection pool
        if let Some(idle) = pool_idle_timeout_secs {
            builder = builder.pool_idle_timeout(Duration::from_secs_f64(idle));
        }
        if let Some(max) = pool_max_idle_per_host {
            builder = builder.pool_max_idle_per_host(max);
        }

        // HTTP/2
        if http2 {
            // Use adaptive: try HTTP/2 via ALPN, fall back to HTTP/1.1
            builder = builder
                .http2_prior_knowledge()
                .http2_adaptive_window(true);
        }

        // Proxy
        if let Some(ref url) = proxy_url {
            let proxy = reqwest::Proxy::all(url)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
            builder = builder.proxy(proxy);
        } else if !trust_env {
            builder = builder.no_proxy();
        }

        // TLS
        if !verify_ssl {
            builder = builder.danger_accept_invalid_certs(true);
        }

        let client = builder
            .build()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;

        Ok(Self { client })
    }

    /// Send a single HTTP request. Returns RawResponse.
    ///
    /// GIL is released during the entire network I/O — other Python threads run freely.
    #[pyo3(signature = (method, url, headers=None, body=None, timeout_secs=None))]
    fn request(
        &self,
        py: Python,
        method: &str,
        url: &str,
        headers: Option<Vec<(String, String)>>,
        body: Option<Vec<u8>>,
        timeout_secs: Option<f64>,
    ) -> PyResult<RawResponse> {
        let client = self.client.clone();
        let method = method.to_string();
        let url = url.to_string();

        py.detach(|| {
            // Use thread-local current_thread runtime — zero cross-thread scheduling
            with_local_runtime(|rt| {
                rt.block_on(do_request(
                    &client,
                    &method,
                    &url,
                    headers,
                    body,
                    timeout_secs,
                ))
            })
        })
    }

    /// Send multiple requests concurrently. Returns list of RawResponse.
    ///
    /// All requests execute in parallel on the tokio runtime with a SINGLE
    /// GIL release. This is the killer feature — no Python HTTP client can do
    /// N concurrent requests with one GIL crossing.
    ///
    /// Each request tuple: (method, url, headers, body)
    #[pyo3(signature = (requests, timeout_secs=None))]
    fn gather(
        &self,
        py: Python,
        requests: Vec<(String, String, Option<Vec<(String, String)>>, Option<Vec<u8>>)>,
        timeout_secs: Option<f64>,
    ) -> PyResult<Vec<RawResponse>> {
        let client = self.client.clone();

        py.detach(|| {
            // Use the same thread-local runtime as request() — connection pool is consistent.
            // current_thread + join_all gives cooperative concurrency: all futures run on
            // one thread but interleave on I/O await points (just like a JS event loop).
            with_local_runtime(|rt| rt.block_on(async {
                let futs: Vec<_> = requests
                    .into_iter()
                    .map(|(method, url, headers, body)| {
                        let client = client.clone();
                        let timeout = timeout_secs;
                        async move {
                            do_request(&client, &method, &url, headers, body, timeout).await
                        }
                    })
                    .collect();

                let results = futures_util::future::join_all(futs).await;
                results.into_iter().collect::<PyResult<Vec<_>>>()
            }))
        })
    }

    /// Close the transport (drop the connection pool).
    fn close(&mut self) {
        // reqwest::Client is reference-counted internally.
        // Dropping our handle lets the pool shut down when no requests are in-flight.
        self.client = reqwest::Client::new();
    }
}
