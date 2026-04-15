use pyo3::prelude::*;

/// Server configuration exposed to Python.
#[pyclass]
#[derive(Clone, Debug)]
pub struct ServerConfig {
    #[pyo3(get, set)]
    pub host: String,
    #[pyo3(get, set)]
    pub port: u16,
}

#[pymethods]
impl ServerConfig {
    #[new]
    #[pyo3(signature = (host = "127.0.0.1".to_string(), port = 8000))]
    fn new(host: String, port: u16) -> Self {
        ServerConfig { host, port }
    }
}
