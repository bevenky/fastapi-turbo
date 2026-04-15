import fastapi_rs


def test_rust_hello():
    assert fastapi_rs.rust_hello("world") == "Hello from Rust, world!"


def test_rust_hello_empty():
    assert fastapi_rs.rust_hello("") == "Hello from Rust, !"


def test_core_version():
    assert fastapi_rs.core_version() == "0.1.0"


def test_module_version():
    assert fastapi_rs.__version__ == "0.1.0"
