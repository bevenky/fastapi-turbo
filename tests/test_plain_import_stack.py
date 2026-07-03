"""Third-party stack smoke under the door — the plain-import contract, live.

Contract: line 1 of the app is ``import fastapi_turbo``; EVERYTHING else is
plain ``from fastapi import ...`` / third-party (redis, asyncpg, psycopg,
httpx, websockets) — and it all just works under ``app.run()``.

ONE subprocess boot of a single app whose source has exactly one turbo line,
then each surface is curled:

  * redis-py sync + asyncio
  * asyncpg
  * psycopg3 sync + async
  * BackgroundTasks (drained by the door after the response)
  * StaticFiles (Rust ServeDir)
  * CORSMiddleware (Tower layer)
  * WebSocket echo

Redis/Postgres endpoints skip gracefully when the local service is down, so
the module still validates the framework surfaces on service-less machines.
The import-identity half of the contract lives in
``tests/test_shim_completeness.py``.
"""
import fastapi_turbo  # noqa: F401 — installs compat shim

import asyncio
import socket
import subprocess
import sys
import textwrap
import time

import httpx
import pytest

pytestmark = pytest.mark.requires_loopback

REDIS_URL = "redis://127.0.0.1:6379"
PG_DSN = "host=127.0.0.1 port=5432 dbname=fastapi_turbo_bench user=venky"


def _service_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


REDIS_UP = _service_up(6379)
PG_UP = _service_up(5432)

needs_redis = pytest.mark.skipif(not REDIS_UP, reason="redis not running on 127.0.0.1:6379")
needs_pg = pytest.mark.skipif(not PG_UP, reason="postgres not running on 127.0.0.1:5432")


# The app under test. ONE turbo line; everything else plain fastapi +
# third-party. ``__PORT__`` / ``__STATIC__`` substituted by the fixture.
PLAIN_APP = '''
import fastapi_turbo  # line 1 — the ONLY fastapi_turbo import in this app

import asyncio

from fastapi import BackgroundTasks, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

REDIS_URL = "redis://127.0.0.1:6379"
PG_DSN = "host=127.0.0.1 port=5432 dbname=fastapi_turbo_bench user=venky"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=r"__STATIC__"), name="static")


@app.get("/hello")
def hello():
    return {"message": "hello"}


# ── BackgroundTasks (door drains after response) ─────────────────────
_BG_DONE = []


def _bg_record(item):
    _BG_DONE.append(item)


@app.post("/bg")
def bg(tasks: BackgroundTasks):
    tasks.add_task(_bg_record, "ran")
    return {"queued": True}


@app.get("/bg/done")
def bg_done():
    return {"count": len(_BG_DONE)}


# ── redis-py: sync + asyncio ─────────────────────────────────────────
@app.get("/redis/sync")
def redis_sync():
    import redis

    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    r.set("plainstack:sync", "sync-ok")
    val = r.get("plainstack:sync")
    r.close()
    return {"value": val}


@app.get("/redis/async")
async def redis_async():
    import redis.asyncio as aioredis

    r = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    await r.set("plainstack:async", "async-ok")
    val = await r.get("plainstack:async")
    await r.aclose()
    return {"value": val}


# ── Postgres: asyncpg + psycopg3 sync + psycopg3 async ───────────────
@app.get("/pg/asyncpg")
async def pg_asyncpg():
    import asyncpg

    conn = await asyncpg.connect(
        host="127.0.0.1", port=5432, database="fastapi_turbo_bench", user="venky"
    )
    try:
        val = await conn.fetchval("SELECT 41 + 1")
    finally:
        await conn.close()
    return {"answer": val}


@app.get("/pg/psycopg-sync")
def pg_psycopg_sync():
    import psycopg

    with psycopg.connect(PG_DSN) as conn:
        val = conn.execute("SELECT 41 + 1").fetchone()[0]
    return {"answer": val}


@app.get("/pg/psycopg-async")
async def pg_psycopg_async():
    import psycopg

    async with await psycopg.AsyncConnection.connect(PG_DSN) as conn:
        cur = await conn.execute("SELECT 41 + 1")
        val = (await cur.fetchone())[0]
    return {"answer": val}


# ── WebSocket echo ───────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_echo(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            await ws.send_text(msg)
    except WebSocketDisconnect:
        pass


app.run(host="127.0.0.1", port=__PORT__)
'''


@pytest.fixture(scope="module")
def stack_url(tmp_path_factory):
    """ONE ``app.run()`` boot for the whole module."""
    tmp_path = tmp_path_factory.mktemp("plain_stack")
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "hello.txt").write_text("static-ok\n")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    src = textwrap.dedent(PLAIN_APP).replace("__PORT__", str(port)).replace(
        "__STATIC__", str(static_dir)
    )
    app_file = tmp_path / "plain_stack_app.py"
    app_file.write_text(src)

    proc = subprocess.Popen(
        [sys.executable, str(app_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if proc.poll() is not None:
                    out = proc.stdout.read().decode()
                    err = proc.stderr.read().decode()
                    pytest.fail(f"plain-import app died on boot.\nstdout: {out}\nstderr: {err}")
                time.sleep(0.1)
        else:
            pytest.fail("plain-import app did not open its port in time")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.kill()
        proc.wait()


def test_contract_source_shape():
    """The app source itself honors the contract: exactly one turbo import,
    and it is the first statement."""
    import ast

    tree = ast.parse(textwrap.dedent(PLAIN_APP))
    turbo_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            turbo_imports += [a.name for a in node.names if a.name.split(".")[0] == "fastapi_turbo"]
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "fastapi_turbo":
                turbo_imports.append(f"from {node.module}")
    assert turbo_imports == ["fastapi_turbo"], turbo_imports
    first = tree.body[0]
    assert isinstance(first, ast.Import) and first.names[0].name == "fastapi_turbo", (
        "line 1 must be `import fastapi_turbo`"
    )


def test_hello(stack_url):
    r = httpx.get(f"{stack_url}/hello")
    assert r.status_code == 200
    assert r.json() == {"message": "hello"}


def test_background_tasks(stack_url):
    r = httpx.post(f"{stack_url}/bg")
    assert r.status_code == 200
    assert r.json() == {"queued": True}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if httpx.get(f"{stack_url}/bg/done").json()["count"] >= 1:
            return
        time.sleep(0.05)
    pytest.fail("background task never ran (door did not drain BackgroundTasks)")


def test_staticfiles(stack_url):
    r = httpx.get(f"{stack_url}/static/hello.txt")
    assert r.status_code == 200
    assert r.text == "static-ok\n"


def test_cors_preflight_and_header(stack_url):
    r = httpx.options(
        f"{stack_url}/hello",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "*"

    r = httpx.get(f"{stack_url}/hello", headers={"Origin": "http://example.com"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"


@needs_redis
def test_redis_sync(stack_url):
    r = httpx.get(f"{stack_url}/redis/sync")
    assert r.status_code == 200, r.text
    assert r.json() == {"value": "sync-ok"}


@needs_redis
def test_redis_async(stack_url):
    r = httpx.get(f"{stack_url}/redis/async")
    assert r.status_code == 200, r.text
    assert r.json() == {"value": "async-ok"}


@needs_pg
def test_pg_asyncpg(stack_url):
    r = httpx.get(f"{stack_url}/pg/asyncpg")
    assert r.status_code == 200, r.text
    assert r.json() == {"answer": 42}


@needs_pg
def test_pg_psycopg_sync(stack_url):
    r = httpx.get(f"{stack_url}/pg/psycopg-sync")
    assert r.status_code == 200, r.text
    assert r.json() == {"answer": 42}


@needs_pg
def test_pg_psycopg_async(stack_url):
    r = httpx.get(f"{stack_url}/pg/psycopg-async")
    assert r.status_code == 200, r.text
    assert r.json() == {"answer": 42}


def test_websocket_echo(stack_url):
    import websockets

    ws_url = stack_url.replace("http://", "ws://") + "/ws"

    async def _roundtrip():
        async with websockets.connect(ws_url) as ws:
            await ws.send("plain-import ws")
            return await ws.recv()

    assert asyncio.run(_roundtrip()) == "plain-import ws"


def test_httpx_testclient_plain_import():
    """``fastapi.testclient.TestClient`` (httpx against the real Rust server)
    works on a plain-import app defined right here in the test process."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/tc")
    def tc():
        return {"via": "testclient"}

    with TestClient(app) as client:
        r = client.get("/tc")
        assert r.status_code == 200
        assert r.json() == {"via": "testclient"}
