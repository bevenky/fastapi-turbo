"""Regression: top-level string returns must produce RFC 8259-valid JSON.

The hand-rolled escape in ``src/responses.rs`` previously only mapped
``\\`` and ``"``, producing invalid JSON for any string containing a
control character (\\n, \\t, \\r, \\x00..\\x1f).
"""
from __future__ import annotations

import json

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _roundtrip(payload: str) -> str:
    app = FastAPI()

    @app.get("/s")
    def s() -> str:
        return payload

    with TestClient(app) as cli:
        r = cli.get("/s")
        assert r.status_code == 200, r.status_code
        # json.loads must accept the body without raising.
        decoded = json.loads(r.content)
        return decoded


def test_plain_ascii():
    assert _roundtrip("hello") == "hello"


def test_newline_tab_carriage_return():
    s = "a\nb\tc\rd"
    assert _roundtrip(s) == s


def test_control_characters_escaped():
    s = "\x00\x01\x02\x1f"
    assert _roundtrip(s) == s


def test_embedded_quote_and_backslash():
    s = r'he said \ "hi"'
    assert _roundtrip(s) == s


def test_unicode_surrogates_and_emoji():
    s = "hello 🚀 world — 日本語"
    assert _roundtrip(s) == s


def test_mixed_edge_cases():
    s = "\n\"\\\t\x00weird\x7fstring"
    assert _roundtrip(s) == s
