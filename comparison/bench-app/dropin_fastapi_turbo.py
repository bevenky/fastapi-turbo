"""Drop-in FastAPI app running on fastapi-turbo — matches the example from docs.

All standard FastAPI syntax, all new features enabled:
- @app.exception_handler
- @app.middleware("http")
- HTTPBearer Security
- root_path="/api/v1"
- response_class=HTMLResponse
- set_cookie with positional args
- Pydantic POST body validation
"""

import sys

import fastapi_turbo  # activates shim

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Security
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer
from typing import Annotated

from pydantic import BaseModel


class Item(BaseModel):
    name: str
    price: float


app = FastAPI(title="dropin", root_path="/api/v1")


@app.exception_handler(HTTPException)
async def http_handler(request, exc):
    return JSONResponse({"err": exc.detail}, status_code=exc.status_code)


@app.middleware("http")
async def add_header(request, call_next):
    resp = await call_next(request)
    if hasattr(resp, "headers"):
        resp.headers["x-custom"] = "1"
    return resp


bearer = HTTPBearer(auto_error=False)


@app.get(
    "/items/{item_id}",
    response_description="the item",
    responses={404: {"description": "not found"}},
    tags=["items"],
    summary="Get item",
    openapi_extra={"x-note": "ok"},
)
async def get_item(
    item_id: Annotated[int, Path(..., ge=1)],
    q: Annotated[str | None, Query(max_length=50)] = None,
):
    if item_id == 0:
        raise HTTPException(404, "nope")
    return {"id": item_id, "q": q}


@app.post("/items", status_code=201, response_class=JSONResponse)
async def create(item: Item):
    return {"name": item.name, "price": item.price, "created": True}


@app.get("/me")
async def me(credentials=Security(bearer, scopes=["me"])):
    if credentials is None:
        return {"tok": None}
    return {"tok": credentials.credentials}


@app.get("/html", response_class=HTMLResponse)
def html():
    return "<h1>hi</h1>"


@app.get("/c")
def cookies_endpoint():
    r = JSONResponse({"ok": True})
    r.set_cookie("session", "abc", 3600)
    r.set_cookie("theme", "dark", max_age=86400, secure=True)
    return r


@app.get("/hello")
def hello():
    return {"message": "hello"}


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8500
    app.run("127.0.0.1", port)
