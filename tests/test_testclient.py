"""Phase 9-10 tests: TestClient for fastapi-turbo applications."""


import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

def test_testclient_basic():
    """TestClient GET request works."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/hello")
    def hello():
        return {"message": "hello"}

    with TestClient(app) as client:
        r = client.get("/hello")
        assert r.status_code == 200
        assert r.json() == {"message": "hello"}


def test_testclient_post_json():
    """TestClient POST with JSON body."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    app = FastAPI()

    class Item(BaseModel):
        name: str

    @app.post("/items")
    def create(item: Item):
        return {"name": item.name}

    with TestClient(app) as client:
        r = client.post("/items", json={"name": "test"})
        assert r.status_code == 200
        assert r.json() == {"name": "test"}


def test_testclient_path_params():
    """TestClient with path parameters."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/users/{user_id}")
    def get_user(user_id: int):
        return {"user_id": user_id}

    with TestClient(app) as client:
        r = client.get("/users/42")
        assert r.status_code == 200
        assert r.json() == {"user_id": 42}


def test_testclient_query_params():
    """TestClient with query parameters."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/search")
    def search(q: str, limit: int = 10):
        return {"q": q, "limit": limit}

    with TestClient(app) as client:
        r = client.get("/search?q=python&limit=5")
        assert r.status_code == 200
        assert r.json() == {"q": "python", "limit": 5}


def test_testclient_404():
    """TestClient returns 404 for unknown routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/hello")
    def hello():
        return {"message": "hello"}

    with TestClient(app) as client:
        r = client.get("/nonexistent")
        assert r.status_code == 404


def test_testclient_http_exception():
    """TestClient handles HTTPException."""
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/fail")
    def fail():
        raise HTTPException(status_code=403, detail="Forbidden")

    with TestClient(app) as client:
        r = client.get("/fail")
        assert r.status_code == 403
        assert r.json() == {"detail": "Forbidden"}


def test_testclient_multiple_requests():
    """TestClient supports multiple requests in one session."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/a")
    def a():
        return {"route": "a"}

    @app.get("/b")
    def b():
        return {"route": "b"}

    with TestClient(app) as client:
        r1 = client.get("/a")
        r2 = client.get("/b")
        assert r1.json() == {"route": "a"}
        assert r2.json() == {"route": "b"}


def test_testclient_from_fastapi_import():
    """TestClient importable from fastapi.testclient."""
    from fastapi.testclient import TestClient
    from fastapi.testclient import TestClient as JTC

    assert TestClient is JTC
