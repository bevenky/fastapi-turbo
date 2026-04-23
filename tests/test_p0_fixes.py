"""Tests for all 6 P0 critical feature gap fixes."""

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

import json
import os
import socket
import subprocess
import sys
import textwrap
import time

import httpx
import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server_app(tmp_path):
    """Start a fastapi_turbo server with the given app code, return base_url."""
    procs = []

    def _start(code: str):
        port = _free_port()
        code = code.replace("__PORT__", str(port))
        code = code.replace("__TMP__", str(tmp_path))
        app_file = tmp_path / "app.py"
        app_file.write_text(textwrap.dedent(code))
        proc = subprocess.Popen(
            [sys.executable, str(app_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        procs.append(proc)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
                if proc.poll() is not None:
                    out = proc.stdout.read().decode()
                    err = proc.stderr.read().decode()
                    pytest.fail(f"Server died on startup.\nstdout: {out}\nstderr: {err}")
        else:
            proc.kill()
            pytest.fail("Server did not start in time")
        return f"http://127.0.0.1:{port}"

    yield _start

    for p in procs:
        p.kill()
        p.wait()


# ===========================================================================
# P0 Fix #1: dependency_overrides checked at runtime
# ===========================================================================


class TestDependencyOverrides:
    """Tests that app.dependency_overrides is checked at call time."""

    def test_override_basic(self, server_app):
        """dependency_overrides replaces a dep function at runtime."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            app = FastAPI()

            def get_db():
                return {"db": "real"}

            def override_db():
                return {"db": "test"}

            @app.get("/check")
            def check(db=Depends(get_db)):
                return db

            app.dependency_overrides[get_db] = override_db
            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/check")
        assert r.status_code == 200
        assert r.json() == {"db": "test"}

    def test_override_with_sub_deps(self, server_app):
        """Overrides work on nested dependencies."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            app = FastAPI()

            def get_db():
                return "real_db"

            def get_user(db=Depends(get_db)):
                return {"user": "alice", "db": db}

            def override_db():
                return "mock_db"

            @app.get("/me")
            def me(user=Depends(get_user)):
                return user

            app.dependency_overrides[get_db] = override_db
            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/me")
        assert r.status_code == 200
        data = r.json()
        assert data["db"] == "mock_db"

    def test_override_dict_exists(self):
        """FastAPI has a dependency_overrides dict."""
        from fastapi import FastAPI
        app = FastAPI()
        assert hasattr(app, "dependency_overrides")
        assert isinstance(app.dependency_overrides, dict)

    def test_no_override_uses_original(self, server_app):
        """When no override is set, the original dep runs."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            app = FastAPI()

            def get_db():
                return {"db": "real"}

            @app.get("/check")
            def check(db=Depends(get_db)):
                return db

            # No overrides set
            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/check")
        assert r.status_code == 200
        assert r.json() == {"db": "real"}


# ===========================================================================
# P0 Fix #2: Startup/shutdown events fire
# ===========================================================================


class TestStartupShutdown:
    """Tests that on_event('startup') and on_event('shutdown') handlers run."""

    def test_startup_handler_runs(self, server_app):
        """Startup handler executes before server starts accepting requests."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI
            app = FastAPI()
            app.state.started = False

            @app.on_event("startup")
            def on_startup():
                app.state.started = True

            @app.get("/check")
            def check():
                return {"started": app.state.started}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/check")
        assert r.status_code == 200
        assert r.json() == {"started": True}

    def test_async_startup_handler_runs(self, server_app):
        """Async startup handler executes before server starts."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI
            app = FastAPI()
            app.state.async_started = False

            @app.on_event("startup")
            async def on_startup():
                app.state.async_started = True

            @app.get("/check")
            def check():
                return {"async_started": app.state.async_started}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/check")
        assert r.status_code == 200
        assert r.json() == {"async_started": True}

    def test_multiple_startup_handlers(self, server_app):
        """Multiple startup handlers all execute in order."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI
            app = FastAPI()
            app.state.steps = []

            @app.on_event("startup")
            def on_startup_1():
                app.state.steps.append("first")

            @app.on_event("startup")
            def on_startup_2():
                app.state.steps.append("second")

            @app.get("/check")
            def check():
                return {"steps": app.state.steps}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/check")
        assert r.status_code == 200
        assert r.json() == {"steps": ["first", "second"]}

    def test_shutdown_handler_registered(self):
        """Shutdown handlers are stored in _on_shutdown list."""
        from fastapi import FastAPI
        import warnings
        app = FastAPI()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            @app.on_event("shutdown")
            def on_shutdown():
                pass

        assert len(app._on_shutdown) == 1

    def test_on_event_returns_decorator(self):
        """on_event returns the original function."""
        from fastapi import FastAPI
        import warnings
        app = FastAPI()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            @app.on_event("startup")
            def my_handler():
                pass

        assert my_handler is not None
        assert callable(my_handler)


# ===========================================================================
# P0 Fix #3: Lifespan context manager invoked
# ===========================================================================


class TestLifespan:
    """Tests that the lifespan context manager is invoked."""

    def test_lifespan_startup(self, server_app):
        """Lifespan startup phase sets state on the app."""
        url = server_app("""
            from contextlib import asynccontextmanager
            import fastapi_turbo  # noqa: F401 — installs compat shim
            from fastapi import FastAPI

            @asynccontextmanager
            async def lifespan(app):
                app.state.db = "connected"
                yield
                app.state.db = "disconnected"

            app = FastAPI(lifespan=lifespan)

            @app.get("/check")
            def check():
                return {"db": app.state.db}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/check")
        assert r.status_code == 200
        assert r.json() == {"db": "connected"}

    def test_lifespan_with_state_dict(self, server_app):
        """Lifespan that yields a state dict populates app.state."""
        url = server_app("""
            from contextlib import asynccontextmanager
            import fastapi_turbo  # noqa: F401 — installs compat shim
            from fastapi import FastAPI

            @asynccontextmanager
            async def lifespan(app):
                yield {"pool": "active", "cache": "ready"}

            app = FastAPI(lifespan=lifespan)

            @app.get("/check")
            def check():
                return {"pool": app.state.pool, "cache": app.state.cache}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/check")
        assert r.status_code == 200
        data = r.json()
        assert data["pool"] == "active"
        assert data["cache"] == "ready"

    def test_lifespan_stored(self):
        """The lifespan callable is stored on the app."""
        from fastapi import FastAPI
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def my_lifespan(app):
            yield

        app = FastAPI(lifespan=my_lifespan)
        assert app.lifespan is my_lifespan

    def test_lifespan_none_by_default(self):
        """Lifespan is None by default."""
        from fastapi import FastAPI
        app = FastAPI()
        assert app.lifespan is None


# ===========================================================================
# P0 Fix #4: Yield dependencies (generator cleanup)
# ===========================================================================


class TestYieldDependencies:
    """Tests that generator (yield) dependencies work with cleanup."""

    def test_generator_dep_basic(self, server_app):
        """Basic generator dep yields a value to the handler."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            app = FastAPI()

            def get_db():
                yield "db_connection"

            @app.get("/check")
            def check(db=Depends(get_db)):
                return {"db": db}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/check")
        assert r.status_code == 200
        assert r.json() == {"db": "db_connection"}

    def test_generator_dep_cleanup_runs(self, server_app):
        """Generator dep cleanup code runs after the handler."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            import json
            app = FastAPI()

            cleanup_log = []

            def get_resource():
                cleanup_log.append("opened")
                yield "resource"
                cleanup_log.append("closed")

            @app.get("/use")
            def use_resource(res=Depends(get_resource)):
                return {"resource": res}

            @app.get("/log")
            def get_log():
                return {"log": cleanup_log}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        # Use the resource
        r = httpx.get(f"{url}/use")
        assert r.status_code == 200
        assert r.json() == {"resource": "resource"}

        # Check the cleanup log
        r = httpx.get(f"{url}/log")
        assert r.status_code == 200
        log = r.json()["log"]
        assert "opened" in log
        assert "closed" in log

    def test_generator_dep_with_params(self, server_app):
        """Generator dep that consumes query parameters."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            app = FastAPI()

            def get_session(user_id: int = 0):
                yield {"user_id": user_id}

            @app.get("/session")
            def session(s=Depends(get_session)):
                return {"user_id": s["user_id"]}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/session?user_id=42")
        assert r.status_code == 200
        data = r.json()
        assert data["user_id"] == 42


# ===========================================================================
# P0 Fix #5: response_model implemented
# ===========================================================================


class TestResponseModel:
    """Tests that response_model filters the handler result."""

    def test_response_model_filters_extra_fields(self, server_app):
        """response_model removes fields not in the model."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI
            from pydantic import BaseModel

            app = FastAPI()

            class UserOut(BaseModel):
                name: str
                email: str

            @app.get("/user", response_model=UserOut)
            def get_user():
                return {"name": "Alice", "email": "alice@example.com", "password": "secret123"}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/user")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Alice"
        assert data["email"] == "alice@example.com"
        assert "password" not in data

    def test_response_model_with_deps(self, server_app):
        """response_model works alongside dependency injection."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            from pydantic import BaseModel

            app = FastAPI()

            class ItemOut(BaseModel):
                name: str

            def get_db():
                return "connected"

            @app.get("/item", response_model=ItemOut)
            def get_item(db=Depends(get_db)):
                return {"name": "Widget", "internal_id": 42, "db": db}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/item")
        assert r.status_code == 200
        data = r.json()
        assert data == {"name": "Widget"}
        assert "internal_id" not in data
        assert "db" not in data

    def test_response_model_stored_on_route(self):
        """response_model is stored on the APIRoute."""
        from fastapi import FastAPI
        from pydantic import BaseModel

        class Out(BaseModel):
            name: str

        app = FastAPI()

        @app.get("/test", response_model=Out)
        def test_route():
            return {"name": "test"}

        route = app.router.routes[0]
        assert route.response_model is Out

    def test_response_model_none_by_default(self):
        """response_model defaults to None."""
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/test")
        def test_route():
            return {"name": "test"}

        route = app.router.routes[0]
        assert route.response_model is None


# ===========================================================================
# P0 Fix #6: Global/router-level dependencies enforced
# ===========================================================================


class TestGlobalDependencies:
    """Tests that app-level and router-level dependencies are enforced."""

    def test_app_level_dependency_runs(self, server_app):
        """App-level dependency runs for every route."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            app = FastAPI()

            call_count = 0

            def track_request():
                global call_count
                call_count += 1

            app.dependencies = [Depends(track_request)]

            @app.get("/a")
            def route_a():
                return {"count": call_count}

            @app.get("/b")
            def route_b():
                return {"count": call_count}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r1 = httpx.get(f"{url}/a")
        assert r1.status_code == 200
        count_a = r1.json()["count"]
        assert count_a >= 1

        r2 = httpx.get(f"{url}/b")
        assert r2.status_code == 200
        count_b = r2.json()["count"]
        assert count_b >= 2

    def test_router_level_dependency_runs(self, server_app):
        """Router-level dependency runs for routes on that router."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            from fastapi.routing import APIRouter
            app = FastAPI()

            auth_log = []

            def verify_auth():
                auth_log.append("checked")

            router = APIRouter(dependencies=[Depends(verify_auth)])

            @router.get("/protected")
            def protected():
                return {"auth_checks": len(auth_log)}

            app.include_router(router, prefix="/api")

            @app.get("/public")
            def public():
                return {"auth_checks": len(auth_log)}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        # Protected route should trigger the router dep
        r = httpx.get(f"{url}/api/protected")
        assert r.status_code == 200
        checks = r.json()["auth_checks"]
        assert checks >= 1

    def test_app_dependencies_constructor(self):
        """FastAPI constructor accepts dependencies parameter."""
        from fastapi import FastAPI, Depends

        def my_dep():
            pass

        app = FastAPI(dependencies=[Depends(my_dep)])
        assert len(app.dependencies) == 1

    def test_router_dependencies_constructor(self):
        """APIRouter constructor accepts dependencies parameter."""
        from fastapi.routing import APIRouter
        from fastapi import Depends

        def my_dep():
            pass

        router = APIRouter(dependencies=[Depends(my_dep)])
        assert len(router.dependencies) == 1

    def test_route_level_dependency(self, server_app):
        """Route-level dependencies (on the decorator) are enforced."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            app = FastAPI()

            dep_log = []

            def log_access():
                dep_log.append("accessed")

            @app.get("/tracked", dependencies=[Depends(log_access)])
            def tracked():
                return {"accesses": len(dep_log)}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/tracked")
        assert r.status_code == 200
        assert r.json()["accesses"] >= 1

    def test_combined_app_router_route_deps(self, server_app):
        """All three levels of dependencies run together."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Depends
            from fastapi.routing import APIRouter
            app = FastAPI()

            log = []

            def app_dep():
                log.append("app")

            def router_dep():
                log.append("router")

            def route_dep():
                log.append("route")

            app.dependencies = [Depends(app_dep)]
            router = APIRouter(dependencies=[Depends(router_dep)])

            @router.get("/test", dependencies=[Depends(route_dep)])
            def test_route():
                return {"log": log}

            app.include_router(router, prefix="/api")
            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/api/test")
        assert r.status_code == 200
        log = r.json()["log"]
        assert "app" in log
        assert "router" in log
        assert "route" in log
