use pyo3::prelude::*;

mod config;
mod handler_bridge;
mod responses;
mod router;
mod server;
mod streaming;
mod websocket;

/// Returns a greeting from Rust.
#[pyfunction]
fn rust_hello(name: &str) -> String {
    format!("Hello from Rust, {}!", name)
}

/// Returns the version of the Rust core.
#[pyfunction]
fn core_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// The PyO3 module definition.
#[pymodule(gil_used = false)]
fn _fastapi_rs_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rust_hello, m)?)?;
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    m.add_function(wrap_pyfunction!(server::run_server, m)?)?;
    m.add_class::<config::ServerConfig>()?;
    m.add_class::<router::RouteInfo>()?;
    m.add_class::<router::ParamInfo>()?;
    m.add_class::<websocket::PyWebSocket>()?;
    m.add_class::<websocket::ChannelAwaitable>()?;
    Ok(())
}
