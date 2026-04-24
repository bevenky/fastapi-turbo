"""Regression: every scalar/collection return type must produce a
response whose body can be parsed by ``json.loads`` and that
round-trips the original value (modulo documented coercions).

This covers the hand-rolled JSON paths in ``src/responses.rs`` for
strings, ints, floats, bools, None, lists, dicts, dataclasses, and
Pydantic models. Unicode + control chars are the most likely
offenders.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


EDGE_STRINGS = [
    "",
    "hello",
    "a\nb",
    "a\tb",
    "a\rb",
    "\x00\x01\x02\x1f\x7f",  # control chars
    'he said \\ "hi"',        # backslash + quote
    "🚀 日本語 — unicode",
    "  ",           # line/paragraph separators
    "a" * 10000,              # large
]


@pytest.mark.parametrize("payload", EDGE_STRINGS)
def test_str_return(payload):
    app = FastAPI()

    @app.get("/s")
    def s() -> str:
        return payload

    with TestClient(app) as cli:
        r = cli.get("/s")
        assert r.status_code == 200
        assert json.loads(r.content) == payload


def test_int_return():
    app = FastAPI()

    @app.get("/n")
    def n() -> int:
        return 42

    with TestClient(app) as cli:
        assert json.loads(cli.get("/n").content) == 42


def test_float_return():
    app = FastAPI()

    @app.get("/f")
    def f() -> float:
        return 3.14159

    with TestClient(app) as cli:
        assert abs(json.loads(cli.get("/f").content) - 3.14159) < 1e-9


def test_bool_return():
    app = FastAPI()

    @app.get("/b")
    def b() -> bool:
        return True

    with TestClient(app) as cli:
        assert json.loads(cli.get("/b").content) is True


def test_none_return():
    app = FastAPI()

    @app.get("/x")
    def x() -> None:
        return None

    with TestClient(app) as cli:
        assert json.loads(cli.get("/x").content) is None


def test_dict_with_control_chars():
    app = FastAPI()

    @app.get("/d")
    def d():
        return {"key\n": "value\t\x00"}

    with TestClient(app) as cli:
        r = cli.get("/d")
        assert r.status_code == 200
        parsed = json.loads(r.content)
        assert parsed == {"key\n": "value\t\x00"}


def test_list_of_weird_strings():
    app = FastAPI()

    @app.get("/l")
    def l():
        return ["a\nb", "c\td", "\x00"]

    with TestClient(app) as cli:
        parsed = json.loads(cli.get("/l").content)
        assert parsed == ["a\nb", "c\td", "\x00"]


def test_dataclass_return():
    @dataclasses.dataclass
    class Point:
        x: int
        y: int
        label: str

    app = FastAPI()

    @app.get("/p")
    def p():
        return Point(1, 2, "origin\nwith newline")

    with TestClient(app) as cli:
        r = cli.get("/p")
        assert r.status_code == 200
        parsed = json.loads(r.content)
        assert parsed == {"x": 1, "y": 2, "label": "origin\nwith newline"}


def test_pydantic_model_return():
    class Item(BaseModel):
        name: str
        qty: int

    app = FastAPI()

    @app.get("/m")
    def m() -> Item:
        return Item(name="widget\n", qty=5)

    with TestClient(app) as cli:
        parsed = json.loads(cli.get("/m").content)
        assert parsed == {"name": "widget\n", "qty": 5}
