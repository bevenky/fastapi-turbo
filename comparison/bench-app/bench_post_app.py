"""Benchmark POST with Pydantic validation — all features enabled."""

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

import sys

from pydantic import BaseModel

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


class Item(BaseModel):
    name: str
    price: float


def build_app(mode: str = "all") -> FastAPI:
    if mode == "all":
        app = FastAPI()

        @app.exception_handler(HTTPException)
        async def handle(request, exc):
            return JSONResponse({"err": exc.detail}, status_code=exc.status_code)

        @app.middleware("http")
        async def mw(request, call_next):
            r = await call_next(request)
            if hasattr(r, "headers"):
                r.headers["x-mw"] = "y"
            return r
    else:
        app = FastAPI()

    @app.post("/items")
    def create(item: Item):
        return {"name": item.name, "price": item.price, "created": True}

    return app


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8400
    build_app(mode).run("127.0.0.1", port)
