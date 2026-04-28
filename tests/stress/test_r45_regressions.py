"""R45 audit follow-ups — async-generator handler auto-streaming
(NDJSON default + ``response_class`` override for SSE), and
yield-dep that swallows handler exceptions raises ``FastAPIError``
(matches FA's ``raising an exception and a dependency with yield
without raising again`` contract). Net sandboxed-gate change:
119 → 112 failed.
"""
from typing import AsyncIterable

import pytest
from pydantic import BaseModel

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 async-generator handler auto-wraps as NDJSON
# ────────────────────────────────────────────────────────────────────


def test_async_generator_handler_emits_ndjson():
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    class _Item(BaseModel):
        name: str

    app = FastAPI()

    @app.get("/stream")
    async def _h() -> AsyncIterable[_Item]:
        for n in ("a", "b", "c"):
            yield _Item(name=n)

    with TestClient(app, in_process=True) as c:
        r = c.get("/stream")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/jsonl", r.headers
        # One JSON object per line.
        lines = [
            ln for ln in r.text.split("\n") if ln.strip()
        ]
        assert lines == [
            '{"name":"a"}',
            '{"name":"b"}',
            '{"name":"c"}',
        ], r.text


def test_sync_generator_handler_emits_ndjson():
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/g")
    def _h():
        yield {"i": 1}
        yield {"i": 2}

    with TestClient(app, in_process=True) as c:
        r = c.get("/g")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/jsonl"


# ────────────────────────────────────────────────────────────────────
# #2 response_class drives streaming framing (e.g. SSE)
# ────────────────────────────────────────────────────────────────────


def test_response_class_overrides_ndjson_default():
    """When the user pins a streaming response_class on the route,
    the dispatcher hands the generator to that class instead of
    forcing NDJSON. Without this, SSE / custom streamers would
    always be wrapped in NDJSON encoding."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.responses import StreamingResponse
    from fastapi_turbo.testclient import TestClient

    class _MyStream(StreamingResponse):
        media_type = "text/event-stream; charset=utf-8"

    app = FastAPI()

    @app.get("/sse", response_class=_MyStream)
    async def _h():
        yield b"data: hello\n\n"
        yield b"data: world\n\n"

    with TestClient(app, in_process=True) as c:
        r = c.get("/sse")
        assert r.status_code == 200, r.text
        assert "text/event-stream" in r.headers["content-type"], r.headers
        assert "data: hello" in r.text
        assert "data: world" in r.text


# ────────────────────────────────────────────────────────────────────
# #3 yield-dep that swallows raises FastAPIError
# ────────────────────────────────────────────────────────────────────


def test_yield_dep_swallow_raises_fastapi_error():
    """Per FA's contract: a yield-dep that catches the handler's
    exception WITHOUT re-raising is a developer bug — surface it as
    a ``FastAPIError`` so the user sees the warning. The dispatcher
    detects this by inspecting whether ``gen.throw(exc)`` returns
    via ``StopIteration`` (silently exhausted) vs re-raises (the
    expected path)."""
    from fastapi_turbo import Depends, FastAPI
    from fastapi_turbo.exceptions import FastAPIError
    from fastapi_turbo.testclient import TestClient

    class _BadErr(Exception):
        pass

    def dep_swallow():
        try:
            yield "ok"
        except _BadErr:
            # Catches but doesn't re-raise — FA forbids this and
            # raises FastAPIError when detected.
            pass

    app = FastAPI()

    @app.get("/x")
    def _h(_v: str = Depends(dep_swallow)):
        raise _BadErr()

    with TestClient(app, in_process=True) as c:
        with pytest.raises(FastAPIError) as exc_info:
            c.get("/x")
        assert "raising an exception and a dependency with yield" in str(
            exc_info.value
        )


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
