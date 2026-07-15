"""P0 FastAPI parity pins NOT covered by the upstream suite.

CONSOLIDATION (coverage-differential, pool-1): baseline = retained local
suite + upstream FastAPI 0.138.1 suite under the shim, per-test coverage
contexts over ``python/fastapi_turbo`` + grep evidence in the 0.138.1 clone.
Deleted-as-redundant (every deleted test had an EMPTY unique-line
differential AND a named twin):

  * response_model alias honored by default / by_alias=False
                                → tests/test_response_by_alias.py
                                  (dict/model/list routes, both polarities)
  * default_response_class cascade (app → router → route override)
                                → tests/test_default_response_class.py
                                  (full nested include_router cascade)
  * FastAPI(responses=) app-level defaults + route override
                                → tests/test_include_router_defaults_overrides.py
                                  (app+router+route responses cascade) +
                                  tests/test_additional_responses_union_
                                  duplicate_anyof.py (FastAPI(responses={...}))
  * ORJSONResponse renders      → tests/test_orjson_response_class.py +
                                  tests/test_deprecated_responses.py
  * JSONResponse works without orjson
                                → retained compact-bytes pin below (strictly
                                  stronger byte assertion; real Starlette
                                  JSONResponse never imports orjson) +
                                  upstream JSONResponse renders suite-wide

KEPT: TestDebugMode in full (engine-printed traceback on stderr is a
turbo Rust-path error shape; FastAPI(debug=) has no upstream-suite test at
all — grep ``debug=True`` over the 0.138.1 clone tests is empty), the
serialization_alias response pin (upstream only exercises request-side /
schema-side serialization_alias), and the compact-bytes pin (upstream's only
byte-level twin lives in tests/benchmarks/test_general_performance.py, which
the compat gate --ignores).
"""

from __future__ import annotations

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`


# ── response_model serialization_alias ───────────────────────────────


class TestResponseModelSerializationAlias:
    def test_serialization_alias(self):
        """Field(serialization_alias=) should also be honored in output."""
        from pydantic import BaseModel, Field

        from fastapi import FastAPI

        class M(BaseModel):
            name: str = Field(serialization_alias="displayName")

        app = FastAPI()

        @app.get("/m", response_model=M)
        def get_m():
            return {"name": "Alice"}

        from fastapi_turbo.testclient import TestClient
        result = TestClient(app, in_process=True).get("/m").json()
        assert "displayName" in result


# ── FastAPI(debug=) ──────────────────────────────────────────────────


class TestDebugMode:
    def test_debug_default_false(self):
        from fastapi import FastAPI

        app = FastAPI()
        assert app.debug is False

    def test_debug_true_stored(self):
        from fastapi import FastAPI

        app = FastAPI(debug=True)
        assert app.debug is True

    def test_debug_prints_traceback(self, capfd):
        """Handler exceptions surface a traceback on stderr (engine-printed)."""
        from fastapi import FastAPI

        app = FastAPI(debug=True)

        @app.get("/boom")
        def boom():
            raise ValueError("something broke")

        from fastapi_turbo.testclient import TestClient

        resp = TestClient(
            app, in_process=True, raise_server_exceptions=False
        ).get("/boom")
        assert resp.status_code == 500
        captured = capfd.readouterr()
        # Traceback should include the error type and message
        assert "ValueError" in captured.err
        assert "something broke" in captured.err

    def test_no_debug_no_traceback(self, capsys):
        """Without debug, tracebacks are NOT printed to stderr."""
        from fastapi import FastAPI

        app = FastAPI(debug=False)

        @app.get("/boom")
        def boom():
            raise ValueError("silent error")

        routes = app._collect_all_routes()
        try:
            routes[0]["endpoint"]()
        except ValueError:
            pass
        captured = capsys.readouterr()
        assert "silent error" not in captured.err

    def test_http_exception_not_traced_in_debug(self, capsys):
        """HTTPException in debug mode should NOT print a traceback — it's control flow."""
        from fastapi import FastAPI, HTTPException

        app = FastAPI(debug=True)

        @app.get("/nf")
        def nf():
            raise HTTPException(status_code=404, detail="nope")

        routes = app._collect_all_routes()
        try:
            routes[0]["endpoint"]()
        except HTTPException:
            pass
        captured = capsys.readouterr()
        # HTTPException is normal control flow, not a bug — no traceback
        assert "HTTPException" not in captured.err


# ── JSONResponse byte-level rendering ────────────────────────────────


class TestJSONResponseBytes:
    def test_json_response_compact_bytes_match_starlette(self):
        """JSONResponse output bytes should match Starlette's compact form."""
        from fastapi.responses import JSONResponse

        resp = JSONResponse(content={"a": 1, "b": "x"})
        # No spaces between key/value (compact form)
        assert resp.body == b'{"a":1,"b":"x"}'
