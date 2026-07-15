"""Regression: runtime deps used by ``TestClient`` / WebSocket / form
parsing must be in ``project.dependencies`` so a fresh ``pip install
fastapi-turbo`` yields a runnable install.

Previously only Pydantic was declared; the rest (httpx, websockets,
python-multipart) were installed incidentally via dev extras.
"""
from __future__ import annotations

import pathlib
try:
    import tomllib  # stdlib on 3.11+
except ImportError:  # Python 3.10 — the pyproject floor
    import tomli as tomllib  # type: ignore[no-redef]


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


def test_db_extra_removed():
    """The ``db`` extra (psycopg/redis) was removed with the
    out-of-scope ``fastapi_turbo.db`` add-on — FastAPI ships no DB
    pools. See docs/archive/STRATEGY.md (bucket 2 — non-FastAPI add-ons)."""
    extras = _read_pyproject()["project"]["optional-dependencies"]
    assert "db" not in extras, "db extra should be gone with the db add-on"


def test_templates_extra_has_jinja():
    extras = _read_pyproject()["project"]["optional-dependencies"]
    assert any(d.startswith("jinja2") for d in extras.get("templates", []))


def test_all_meta_extra_is_superset():
    """`all` should bundle every (remaining) optional extra so `pip install
    fastapi-turbo[all]` gets everything. psycopg/redis were dropped with
    the db add-on; `all` now bundles templates + the json accelerators."""
    extras = _read_pyproject()["project"]["optional-dependencies"]
    all_set = {d.split(">=")[0].split("[")[0] for d in extras.get("all", [])}
    assert "jinja2" in all_set
    assert "psycopg" not in all_set
    assert "redis" not in all_set
