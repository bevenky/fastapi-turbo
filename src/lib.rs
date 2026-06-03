#![allow(
    // Trade-offs we've made intentionally:
    clippy::too_many_arguments,       // request handlers wrap many bits of context
    clippy::result_large_err,         // pyo3::PyErr is the project-wide error type
    clippy::type_complexity,          // FnMut closure types are unavoidable
    clippy::unnecessary_cast,         // clippy confuses f64-as-f64 in percentile math
    clippy::manual_strip,             // a few paths use starts_with + &s[n..] for clarity
    clippy::needless_return,          // `return` in early-exit arms reads more clearly
    clippy::only_used_in_recursion,   // recursive helpers use their own params
)]

use pyo3::prelude::*;

mod cluster;
mod config;
mod handler_bridge;
mod multipart;
mod responses;
mod router;
mod server;
mod streaming;
mod websocket;

/// Returns the version of the Rust core.
#[pyfunction]
fn core_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// The PyO3 module definition.
#[pymodule(gil_used = false)]
fn _fastapi_turbo_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    m.add_function(wrap_pyfunction!(server::run_server, m)?)?;
    m.add_function(wrap_pyfunction!(server::request_server_shutdown, m)?)?;
    // The in-process ASGI door (second door into the one engine).
    m.add_function(wrap_pyfunction!(server::register_app_router, m)?)?;
    m.add_function(wrap_pyfunction!(server::process_request, m)?)?;
    m.add_function(wrap_pyfunction!(server::process_request_streaming, m)?)?;
    m.add_function(wrap_pyfunction!(server::_oneshot_selftest, m)?)?;
    // Multi-worker mode (fd-passing acceptor + workers).
    m.add_function(wrap_pyfunction!(server::run_acceptor, m)?)?;
    m.add_function(wrap_pyfunction!(server::run_worker, m)?)?;
    m.add_class::<config::ServerConfig>()?;
    m.add_class::<router::RouteInfo>()?;
    m.add_class::<router::ParamInfo>()?;
    m.add_class::<websocket::PyWebSocket>()?;
    m.add_class::<websocket::ChannelAwaitable>()?;
    m.add_class::<websocket::TextAwaitable>()?;
    m.add_class::<websocket::BytesAwaitable>()?;
    m.add_class::<websocket::CloseAwaitable>()?;
    m.add_class::<multipart::PyUploadFile>()?;
    m.add_class::<multipart::PySyncFile>()?;
    m.add_class::<multipart::ImmediateBytes>()?;
    m.add_class::<multipart::ImmediateNone>()?;
    m.add_class::<server::PyResponseStream>()?;
    Ok(())
}
