"""Regression: runtime deps used by ``TestClient`` / WebSocket / form
parsing must be in ``project.dependencies`` so a fresh ``pip install
fastapi-turbo`` yields a runnable install.

Previously only Pydantic was declared; the rest (httpx, websockets,
python-multipart) were installed incidentally via dev extras.
"""
from __future__ import annotations

import pathlib
import tomllib


def _read_pyproject():
    p = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
    return tomllib.loads(p.read_text())


def test_httpx_is_a_runtime_dep():
    deps = _read_pyproject()["project"]["dependencies"]
    assert any(d.split(">=")[0].split("[")[0] == "httpx" for d in deps), deps


def test_websockets_is_a_runtime_dep():
    deps = _read_pyproject()["project"]["dependencies"]
    assert any(d.split(">=")[0].split("[")[0] == "websockets" for d in deps), deps


def test_python_multipart_is_a_runtime_dep():
    deps = _read_pyproject()["project"]["dependencies"]
    assert any(
        d.split(">=")[0].split("[")[0] == "python-multipart" for d in deps
    ), deps


def test_db_extra_exposes_psycopg_and_redis():
    extras = _read_pyproject()["project"]["optional-dependencies"]
    db = extras.get("db", [])
    assert any(d.startswith("psycopg") for d in db), db
    assert any(d.startswith("redis") for d in db), db


def test_templates_extra_has_jinja():
    extras = _read_pyproject()["project"]["optional-dependencies"]
    assert any(d.startswith("jinja2") for d in extras.get("templates", []))


def test_all_meta_extra_is_superset():
    """`all` should bundle every optional extra so `pip install
    fastapi-turbo[all]` gets everything."""
    extras = _read_pyproject()["project"]["optional-dependencies"]
    all_set = {d.split(">=")[0].split("[")[0] for d in extras.get("all", [])}
    assert "jinja2" in all_set
    assert "psycopg" in all_set
    assert "redis" in all_set
