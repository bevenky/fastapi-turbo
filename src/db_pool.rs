//! Rust-native PostgreSQL connection pool exposed to Python.
//!
//! Uses tokio-postgres + bb8 for async connection pooling.
//! All DB I/O happens in Rust on the tokio runtime — zero GIL during queries.
//! GIL is only acquired to convert results to Python dicts.
//!
//! Usage from Python:
//!   from fastapi_turbo.db import Pool
//!   pool = Pool("postgresql://user@localhost/mydb")
//!   row = pool.query_one("SELECT * FROM users WHERE id=$1", [1])
//!   rows = pool.query("SELECT * FROM users LIMIT $1", [10])
//!   r1, r2, r3 = pool.gather(
//!       ("SELECT * FROM users WHERE id=$1", [1]),
//!       ("SELECT * FROM orders WHERE uid=$1", [1]),
//!       ("SELECT COUNT(*) FROM orders WHERE uid=$1", [1]),
//!   )

use bb8::Pool;
use bb8_postgres::PostgresConnectionManager;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::sync::Arc;
use tokio::runtime::Handle as TokioHandle;
use tokio_postgres::types::ToSql;
use tokio_postgres::{NoTls, Row};

/// Convert a Postgres Row to a Python dict.
fn row_to_pydict(py: Python<'_>, row: &Row) -> PyResult<Py<PyAny>> {
    let dict = PyDict::new(py);
    for (i, col) in row.columns().iter().enumerate() {
        let name = col.name();
        // Try common types
        if let Ok(v) = row.try_get::<_, i32>(i) {
            let _ = dict.set_item(name, v);
        } else if let Ok(v) = row.try_get::<_, i64>(i) {
            let _ = dict.set_item(name, v);
        } else if let Ok(v) = row.try_get::<_, f64>(i) {
            let _ = dict.set_item(name, v);
        } else if let Ok(v) = row.try_get::<_, f32>(i) {
            let _ = dict.set_item(name, v);
        } else if let Ok(v) = row.try_get::<_, String>(i) {
            let _ = dict.set_item(name, v);
        } else if let Ok(v) = row.try_get::<_, bool>(i) {
            let _ = dict.set_item(name, v);
        } else if let Ok(v) = row.try_get::<_, Option<String>>(i) {
            let _ = dict.set_item(name, v);
        } else if let Ok(v) = row.try_get::<_, Option<i32>>(i) {
            let _ = dict.set_item(name, v);
        } else if let Ok(v) = row.try_get::<_, Option<i64>>(i) {
            let _ = dict.set_item(name, v);
        } else if let Ok(v) = row.try_get::<_, Option<f64>>(i) {
            let _ = dict.set_item(name, v);
        } else {
            // Fallback: try as string
            if let Ok(v) = row.try_get::<_, Option<String>>(i) {
                let _ = dict.set_item(name, v);
            } else {
                let _ = dict.set_item(name, py.None());
            }
        }
    }
    Ok(dict.into_any().unbind())
}

/// Convert a Python value to a tokio-postgres parameter.
/// Returns a boxed trait object for dynamic dispatch.
fn py_to_sql(_py: Python<'_>, val: &Bound<'_, PyAny>) -> Box<dyn ToSql + Sync + Send> {
    if let Ok(v) = val.extract::<i32>() {
        Box::new(v)
    } else if let Ok(v) = val.extract::<i64>() {
        Box::new(v)
    } else if let Ok(v) = val.extract::<f64>() {
        Box::new(v)
    } else if let Ok(v) = val.extract::<bool>() {
        Box::new(v)
    } else if let Ok(v) = val.extract::<String>() {
        Box::new(v)
    } else if val.is_none() {
        Box::new(None::<String>)
    } else {
        // Fallback: convert to string
        let s = val.str().map(|s| s.to_string()).unwrap_or_default();
        Box::new(s)
    }
}

#[pyclass]
pub struct PyPool {
    pool: Arc<Pool<PostgresConnectionManager<NoTls>>>,
    rt: TokioHandle,
}

#[pymethods]
impl PyPool {
    /// Create a new connection pool.
    /// dsn: "postgresql://user@localhost/dbname"
    #[new]
    #[pyo3(signature = (dsn, min_size=5, max_size=20))]
    fn new(dsn: &str, min_size: u32, max_size: u32) -> PyResult<Self> {
        let rt = TokioHandle::current();
        let dsn = dsn.to_string();

        let pool = rt.block_on(async move {
            let manager = PostgresConnectionManager::new(
                dsn.parse().map_err(|e| {
                    pyo3::exceptions::PyValueError::new_err(format!("Invalid DSN: {e}"))
                })?,
                NoTls,
            );
            Pool::builder()
                .min_idle(Some(min_size))
                .max_size(max_size)
                .build(manager)
                .await
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Pool creation failed: {e}"))
                })
        })?;

        Ok(PyPool {
            pool: Arc::new(pool),
            rt,
        })
    }

    /// Execute a query and return a single row as a dict.
    /// Returns None if no rows found.
    fn query_one(&self, py: Python<'_>, sql: &str, params: Option<&Bound<'_, PyList>>) -> PyResult<Py<PyAny>> {
        let pool = self.pool.clone();
        let sql = sql.to_string();
        let param_values = extract_params(py, params)?;

        let row = py.detach(|| {
            self.rt.block_on(async {
                let conn = pool.get().await.map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Connection error: {e}"))
                })?;
                let params_ref: Vec<&(dyn ToSql + Sync)> =
                    param_values.iter().map(|p| p.as_ref() as &(dyn ToSql + Sync)).collect();
                conn.query_opt(&sql, &params_ref).await.map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Query error: {e}"))
                })
            })
        })?;

        match row {
            Some(r) => row_to_pydict(py, &r),
            None => Ok(py.None()),
        }
    }

    /// Execute a query and return all rows as a list of dicts.
    fn query(&self, py: Python<'_>, sql: &str, params: Option<&Bound<'_, PyList>>) -> PyResult<Py<PyAny>> {
        let pool = self.pool.clone();
        let sql = sql.to_string();
        let param_values = extract_params(py, params)?;

        let rows = py.detach(|| {
            self.rt.block_on(async {
                let conn = pool.get().await.map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Connection error: {e}"))
                })?;
                let params_ref: Vec<&(dyn ToSql + Sync)> =
                    param_values.iter().map(|p| p.as_ref() as &(dyn ToSql + Sync)).collect();
                conn.query(&sql, &params_ref).await.map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Query error: {e}"))
                })
            })
        })?;

        let list = PyList::empty(py);
        for row in &rows {
            list.append(row_to_pydict(py, row)?)?;
        }
        Ok(list.into_any().unbind())
    }

    /// Execute multiple queries in PARALLEL and return results as a list.
    /// Each query is a tuple of (sql, params_list).
    /// This is the equivalent of Go goroutines — all queries run simultaneously
    /// on the tokio runtime with zero Python/GIL overhead during execution.
    fn gather(&self, py: Python<'_>, queries: &Bound<'_, PyList>) -> PyResult<Py<PyAny>> {
        let pool = self.pool.clone();

        // Extract all queries and params while we have the GIL
        let mut query_specs: Vec<(String, Vec<Box<dyn ToSql + Sync + Send>>)> = Vec::new();
        for item in queries.iter() {
            let tuple = item.cast::<PyTuple>()?;
            let sql: String = tuple.get_item(0)?.extract()?;
            let params = if tuple.len() > 1 {
                let param_list = tuple.get_item(1)?;
                if let Ok(list) = param_list.cast::<PyList>() {
                    list.iter().map(|v| py_to_sql(py, &v)).collect()
                } else {
                    vec![]
                }
            } else {
                vec![]
            };
            query_specs.push((sql, params));
        }

        // Release GIL and run ALL queries in parallel on tokio
        let all_rows = py.detach(|| {
            self.rt.block_on(async {
                let mut handles = Vec::new();

                for (sql, params) in query_specs {
                    let pool = pool.clone();
                    let handle = tokio::spawn(async move {
                        let conn = pool.get().await.map_err(|e| {
                            pyo3::exceptions::PyRuntimeError::new_err(format!("Connection error: {e}"))
                        })?;
                        let params_ref: Vec<&(dyn ToSql + Sync)> =
                            params.iter().map(|p| p.as_ref() as &(dyn ToSql + Sync)).collect();
                        conn.query(&sql, &params_ref).await.map_err(|e| {
                            pyo3::exceptions::PyRuntimeError::new_err(format!("Query error: {e}"))
                        })
                    });
                    handles.push(handle);
                }

                // Wait for ALL queries to complete
                let mut results = Vec::new();
                for handle in handles {
                    let rows = handle.await
                        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("Task error: {e}")))?
                        ?;
                    results.push(rows);
                }
                Ok::<Vec<Vec<Row>>, PyErr>(results)
            })
        })?;

        // Convert results to Python (GIL re-acquired here)
        let outer_list = PyList::empty(py);
        for rows in &all_rows {
            if rows.len() == 1 {
                // Single row — return as dict directly
                outer_list.append(row_to_pydict(py, &rows[0])?)?;
            } else {
                // Multiple rows — return as list of dicts
                let inner_list = PyList::empty(py);
                for row in rows {
                    inner_list.append(row_to_pydict(py, row)?)?;
                }
                outer_list.append(inner_list)?;
            }
        }
        Ok(outer_list.into_any().unbind())
    }

    /// Execute a query that doesn't return rows (INSERT/UPDATE/DELETE).
    /// Returns the number of rows affected.
    fn execute(&self, py: Python<'_>, sql: &str, params: Option<&Bound<'_, PyList>>) -> PyResult<u64> {
        let pool = self.pool.clone();
        let sql = sql.to_string();
        let param_values = extract_params(py, params)?;

        py.detach(|| {
            self.rt.block_on(async {
                let conn = pool.get().await.map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Connection error: {e}"))
                })?;
                let params_ref: Vec<&(dyn ToSql + Sync)> =
                    param_values.iter().map(|p| p.as_ref() as &(dyn ToSql + Sync)).collect();
                conn.execute(&sql, &params_ref).await.map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("Execute error: {e}"))
                })
            })
        })
    }
}

fn extract_params(
    py: Python<'_>,
    params: Option<&Bound<'_, PyList>>,
) -> PyResult<Vec<Box<dyn ToSql + Sync + Send>>> {
    match params {
        Some(list) => Ok(list.iter().map(|v| py_to_sql(py, &v)).collect()),
        None => Ok(vec![]),
    }
}
