import fastapi_turbo


def test_rust_hello():
    assert fastapi_turbo.rust_hello("world") == "Hello from Rust, world!"


def test_rust_hello_empty():
    assert fastapi_turbo.rust_hello("") == "Hello from Rust, !"


def test_core_version():
    assert fastapi_turbo.core_version() == "0.1.0"


def test_module_version():
    assert fastapi_turbo.__version__ == "0.1.0"
