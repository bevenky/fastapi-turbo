"""Benchmark app for NEW features — measure overhead vs baseline.

Endpoints:
  /hello                              — baseline (no new features)
  /hello-html       (response_class)  — response_class=HTMLResponse
  /hello-cookie     (set_cookie)      — Response.set_cookie
  /hello-mw         (middleware)      — @app.middleware('http') registered
  /hello-exc        (exc_handler)     — exception_handler registered
  /hello-all                          — all features combined
"""

import sys

from fastapi_rs import FastAPI, HTTPException
from fastapi_rs.responses import HTMLResponse, JSONResponse, Response

ROOT_PATH = ""  # can be overridden via env for root_path testing


def build_app(mode: str = "all") -> FastAPI:
    if mode == "all":
        app = FastAPI()

        @app.exception_handler(HTTPException)
        async def handle(request, exc):
            return JSONResponse({"err": exc.detail}, status_code=exc.status_code)

        @app.middleware("http")
        async def add_header(request, call_next):
            resp = await call_next(request)
            if hasattr(resp, "headers"):
                resp.headers["x-mw"] = "y"
            return resp

    elif mode == "exc_only":
        app = FastAPI()

        @app.exception_handler(HTTPException)
        async def handle(request, exc):
            return JSONResponse({"err": exc.detail}, status_code=exc.status_code)

    elif mode == "mw_only":
        app = FastAPI()

        @app.middleware("http")
        async def add_header(request, call_next):
            resp = await call_next(request)
            if hasattr(resp, "headers"):
                resp.headers["x-mw"] = "y"
            return resp

    elif mode == "root_path":
        app = FastAPI(root_path="/api/v1")

    else:  # "baseline"
        app = FastAPI()

    @app.get("/hello")
    def hello():
        return {"message": "hello"}

    @app.get("/hello-html", response_class=HTMLResponse)
    def hello_html():
        return "<h1>hi</h1>"

    @app.get("/hello-cookie")
    def hello_cookie():
        r = JSONResponse({"message": "hello"})
        r.set_cookie("session", "abc123")
        return r

    @app.get("/err")
    def err():
        raise HTTPException(status_code=400, detail="test error")

    return app


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8100
    app = build_app(mode)
    app.run(host="127.0.0.1", port=port)
