"""Tests for P1 feature gaps: mount, BaseHTTPMiddleware, StaticFiles,
Jinja2Templates, multiple body params, Body(embed=True),
TrustedHostMiddleware, HTTPSRedirectMiddleware.
"""

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
# 1. app.mount() for sub-applications
# ===========================================================================


class TestMount:
    """Tests for FastAPI.mount()."""

    def test_mount_fastapi_sub_app(self, server_app):
        """Routes from a mounted FastAPI sub-app are reachable with prefix."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI
            app = FastAPI()
            sub = FastAPI()

            @sub.get("/items")
            def items():
                return {"items": ["a", "b"]}

            app.mount("/api/v1", sub)

            @app.get("/health")
            def health():
                return {"status": "ok"}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/api/v1/items")
        assert r.status_code == 200
        assert r.json() == {"items": ["a", "b"]}

        r = httpx.get(f"{url}/health")
        assert r.status_code == 200

    def test_mount_api_router(self, server_app):
        """Mount an APIRouter instance directly."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI
            from fastapi.routing import APIRouter
            app = FastAPI()
            router = APIRouter()

            @router.get("/users")
            def users():
                return {"users": []}

            app.mount("/v2", router)
            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.get(f"{url}/v2/users")
        assert r.status_code == 200
        assert r.json() == {"users": []}

    def test_mount_method_exists(self):
        """FastAPI has a mount method."""
        from fastapi import FastAPI
        app = FastAPI()
        assert hasattr(app, "mount")
        assert callable(app.mount)

    def test_mount_stores_apps(self):
        """mount() stores sub-apps in the _mounts list."""
        from fastapi import FastAPI
        app = FastAPI()
        sub = FastAPI()
        app.mount("/sub", sub, name="sub")
        assert len(app._mounts) == 1
        assert app._mounts[0][0] == "/sub"
        assert app._mounts[0][1] is sub
        assert app._mounts[0][2] == "sub"

    def test_mount_collects_routes(self):
        """_collect_all_routes includes mounted app routes with prefix."""
        from fastapi import FastAPI
        app = FastAPI()
        sub = FastAPI()

        @sub.get("/items")
        def items():
            return []

        app.mount("/api", sub)
        routes = app._collect_all_routes()
        paths = [r["path"] for r in routes]
        assert "/api/items" in paths


# ===========================================================================
# 2. BaseHTTPMiddleware
# ===========================================================================


class TestBaseHTTPMiddleware:
    """Tests for BaseHTTPMiddleware."""

    def test_import_from_middleware(self):
        """BaseHTTPMiddleware is importable from the middleware package."""
        from starlette.middleware import BaseHTTPMiddleware
        assert BaseHTTPMiddleware is not None

    def test_import_from_base_module(self):
        """BaseHTTPMiddleware is importable from middleware.base."""
        from starlette.middleware.base import BaseHTTPMiddleware
        assert BaseHTTPMiddleware is not None

    def test_import_from_starlette_shim(self):
        """Starlette shim exposes BaseHTTPMiddleware."""
        from starlette.middleware.base import BaseHTTPMiddleware
        assert BaseHTTPMiddleware is not None

    def test_subclass_dispatch(self):
        """Subclassing and overriding dispatch works."""
        from starlette.middleware.base import BaseHTTPMiddleware

        class TimingMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                response = await call_next(request)
                return response

        mw = TimingMiddleware()
        assert mw.dispatch_func == mw.dispatch

    def test_dispatch_with_callable(self):
        """Passing a dispatch callable to constructor works."""
        from starlette.middleware.base import BaseHTTPMiddleware

        async def my_dispatch(request, call_next):
            return await call_next(request)

        mw = BaseHTTPMiddleware(dispatch=my_dispatch)
        assert mw.dispatch_func is my_dispatch

    def test_middleware_type_attribute(self):
        """BaseHTTPMiddleware has the _fastapi_turbo_middleware_type attribute."""
        from starlette.middleware.base import BaseHTTPMiddleware
        assert BaseHTTPMiddleware._fastapi_turbo_middleware_type == "base_http"

    def test_default_dispatch_raises(self):
        """Default dispatch raises NotImplementedError."""
        import asyncio
        from starlette.middleware.base import BaseHTTPMiddleware

        mw = BaseHTTPMiddleware()
        with pytest.raises(NotImplementedError):
            asyncio.run(mw.dispatch(None, None))


# ===========================================================================
# 3. StaticFiles
# ===========================================================================


class TestStaticFiles:
    """Tests for StaticFiles."""

    def test_import_from_module(self):
        """StaticFiles is importable from staticfiles module."""
        from fastapi.staticfiles import StaticFiles
        assert StaticFiles is not None

    def test_import_from_starlette_shim(self):
        """Starlette shim exposes StaticFiles."""
        from starlette.staticfiles import StaticFiles
        assert StaticFiles is not None

    def test_constructor_with_directory(self, tmp_path):
        """StaticFiles accepts a directory parameter."""
        from fastapi.staticfiles import StaticFiles
        sf = StaticFiles(directory=str(tmp_path))
        assert sf.directory == str(tmp_path)

    def test_check_dir_raises_on_missing(self):
        """StaticFiles raises RuntimeError when directory does not exist."""
        from fastapi.staticfiles import StaticFiles
        with pytest.raises(RuntimeError, match="does not exist"):
            StaticFiles(directory="/nonexistent/path/xyz123")

    def test_check_dir_false_no_raise(self):
        """StaticFiles with check_dir=False does not raise on missing dir."""
        from fastapi.staticfiles import StaticFiles
        sf = StaticFiles(directory="/nonexistent/path/xyz123", check_dir=False)
        assert sf.directory == "/nonexistent/path/xyz123"

    def test_lookup_path_existing_file(self, tmp_path):
        """lookup_path finds an existing file and returns its media type."""
        from fastapi.staticfiles import StaticFiles

        test_file = tmp_path / "style.css"
        test_file.write_text("body { color: red; }")

        sf = StaticFiles(directory=str(tmp_path))
        path, media_type = sf.lookup_path("style.css")
        assert path == str(test_file)
        assert media_type == "text/css"

    def test_lookup_path_missing_file(self, tmp_path):
        """lookup_path returns empty string for missing files."""
        from fastapi.staticfiles import StaticFiles
        sf = StaticFiles(directory=str(tmp_path))
        path, media_type = sf.lookup_path("nonexistent.txt")
        assert path == ""
        assert media_type is None

    def test_lookup_path_prevents_traversal(self, tmp_path):
        """lookup_path rejects directory traversal attempts."""
        from fastapi.staticfiles import StaticFiles
        sf = StaticFiles(directory=str(tmp_path))
        path, media_type = sf.lookup_path("../../etc/passwd")
        assert path == ""
        assert media_type is None

    def test_html_mode_index(self, tmp_path):
        """In html mode, lookup_path finds index.html for directories."""
        from fastapi.staticfiles import StaticFiles

        index = tmp_path / "index.html"
        index.write_text("<h1>Hello</h1>")

        sf = StaticFiles(directory=str(tmp_path), html=True)
        path, media_type = sf.lookup_path("/")
        assert path == str(index)
        assert media_type == "text/html"

    def test_constructor_kwargs(self):
        """StaticFiles stores html and packages kwargs."""
        from fastapi.staticfiles import StaticFiles
        sf = StaticFiles(directory=None, packages=["mypackage"], html=True, check_dir=False)
        assert sf.html is True
        assert sf.packages == ["mypackage"]


# ===========================================================================
# 4. Jinja2Templates
# ===========================================================================


class TestJinja2Templates:
    """Tests for Jinja2Templates."""

    def test_import_from_module(self):
        """Jinja2Templates is importable from templating module."""
        from fastapi.templating import Jinja2Templates
        assert Jinja2Templates is not None

    def test_import_from_starlette_shim(self):
        """Starlette shim exposes Jinja2Templates."""
        from starlette.templating import Jinja2Templates
        assert Jinja2Templates is not None

    def test_render_template(self, tmp_path):
        """TemplateResponse renders a template and returns HTMLResponse."""
        from fastapi.templating import Jinja2Templates
        from fastapi.responses import HTMLResponse

        tpl_file = tmp_path / "hello.html"
        tpl_file.write_text("<h1>Hello {{ name }}!</h1>")

        templates = Jinja2Templates(directory=str(tmp_path))
        response = templates.TemplateResponse("hello.html", {"name": "World"})

        assert isinstance(response, HTMLResponse)
        assert response.status_code == 200
        assert b"Hello World!" in response.body

    def test_render_with_custom_status_code(self, tmp_path):
        """TemplateResponse respects custom status_code."""
        from fastapi.templating import Jinja2Templates

        tpl_file = tmp_path / "error.html"
        tpl_file.write_text("<h1>Error</h1>")

        templates = Jinja2Templates(directory=str(tmp_path))
        response = templates.TemplateResponse("error.html", {}, status_code=404)
        assert response.status_code == 404

    def test_render_with_headers(self, tmp_path):
        """TemplateResponse passes extra headers."""
        from fastapi.templating import Jinja2Templates

        tpl_file = tmp_path / "page.html"
        tpl_file.write_text("<p>page</p>")

        templates = Jinja2Templates(directory=str(tmp_path))
        response = templates.TemplateResponse(
            "page.html", {}, headers={"x-custom": "value"}
        )
        assert response.headers.get("x-custom") == "value"

    def test_get_template(self, tmp_path):
        """get_template returns a jinja2 Template object."""
        from fastapi.templating import Jinja2Templates

        tpl_file = tmp_path / "test.html"
        tpl_file.write_text("{{ x }}")

        templates = Jinja2Templates(directory=str(tmp_path))
        tpl = templates.get_template("test.html")
        assert tpl.render(x="hello") == "hello"

    def test_template_with_loop(self, tmp_path):
        """Templates support Jinja2 features like for loops."""
        from fastapi.templating import Jinja2Templates

        tpl_file = tmp_path / "list.html"
        tpl_file.write_text("{% for i in items %}{{ i }},{% endfor %}")

        templates = Jinja2Templates(directory=str(tmp_path))
        response = templates.TemplateResponse(
            "list.html", {"items": ["a", "b", "c"]}
        )
        assert response.body == b"a,b,c,"


# ===========================================================================
# 5. Multiple body parameters
# ===========================================================================


class TestMultipleBodyParams:
    """Tests for multiple body parameters support."""

    def test_multiple_body_params_introspection(self):
        """Multiple Pydantic body params are combined into one model."""
        from pydantic import BaseModel
        from fastapi_turbo._introspect import introspect_endpoint

        class Item(BaseModel):
            name: str

        class User(BaseModel):
            username: str

        def handler(item: Item, user: User):
            pass

        params = introspect_endpoint(handler, "/test")
        # Should have a single combined body param
        body_params = [p for p in params if p["kind"] == "body"]
        assert len(body_params) == 1
        assert body_params[0]["name"] == "_combined_body"
        assert body_params[0]["_body_param_names"] == ["item", "user"]

    def test_single_body_no_combine(self):
        """Single Pydantic body param is NOT combined."""
        from pydantic import BaseModel
        from fastapi_turbo._introspect import introspect_endpoint

        class Item(BaseModel):
            name: str

        def handler(item: Item):
            pass

        params = introspect_endpoint(handler, "/test")
        body_params = [p for p in params if p["kind"] == "body"]
        assert len(body_params) == 1
        assert body_params[0]["name"] == "item"

    def test_combined_model_validates(self):
        """The combined model accepts correct data."""
        from pydantic import BaseModel
        from fastapi_turbo._introspect import introspect_endpoint

        class Item(BaseModel):
            name: str

        class User(BaseModel):
            username: str

        def handler(item: Item, user: User):
            pass

        params = introspect_endpoint(handler, "/test")
        body_param = [p for p in params if p["kind"] == "body"][0]
        CombinedModel = body_param["model_class"]

        instance = CombinedModel(
            item={"name": "widget"},
            user={"username": "alice"},
        )
        assert instance.item.name == "widget"
        assert instance.user.username == "alice"

    def test_multiple_body_params_server(self, server_app):
        """Multiple body params work end-to-end via the server."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI
            from pydantic import BaseModel

            app = FastAPI()

            class Item(BaseModel):
                name: str

            class User(BaseModel):
                username: str

            @app.post("/create")
            def create(item: Item, user: User):
                return {"item_name": item.name, "username": user.username}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.post(
            f"{url}/create",
            json={"item": {"name": "widget"}, "user": {"username": "alice"}},
        )
        assert r.status_code == 200
        assert r.json() == {"item_name": "widget", "username": "alice"}


# ===========================================================================
# 6. Body(embed=True)
# ===========================================================================


class TestBodyEmbed:
    """Tests for Body(embed=True) support."""

    def test_body_embed_attribute(self):
        """Body marker has an embed attribute."""
        from fastapi.param_functions import Body
        b = Body(embed=True)
        assert b.embed is True

    def test_body_embed_default_none(self):
        """Body marker defaults embed to None (FastAPI-compatible — means auto-detect)."""
        from fastapi.param_functions import Body
        b = Body()
        assert b.embed is None

    def test_embed_single_body_introspection(self):
        """Single body param with embed=True is combined."""
        from pydantic import BaseModel
        from fastapi_turbo._introspect import introspect_endpoint
        from fastapi.param_functions import Body

        class Item(BaseModel):
            name: str

        def handler(item: Item = Body(embed=True)):
            pass

        params = introspect_endpoint(handler, "/test")
        body_params = [p for p in params if p["kind"] == "body"]
        assert len(body_params) == 1
        assert body_params[0]["name"] == "_combined_body"

    def test_embed_server(self, server_app):
        """Body(embed=True) works end-to-end: expects {"item": {...}}."""
        url = server_app("""
            import fastapi_turbo  # noqa: F401 — installs compat shim

            from fastapi import FastAPI, Body
            from pydantic import BaseModel

            app = FastAPI()

            class Item(BaseModel):
                name: str
                price: float

            @app.post("/items")
            def create_item(item: Item = Body(embed=True)):
                return {"name": item.name, "price": item.price}

            app.run(host="127.0.0.1", port=__PORT__)
        """)
        r = httpx.post(
            f"{url}/items",
            json={"item": {"name": "Widget", "price": 9.99}},
        )
        assert r.status_code == 200
        assert r.json() == {"name": "Widget", "price": 9.99}


# ===========================================================================
# 7. TrustedHostMiddleware
# ===========================================================================


class TestTrustedHostMiddleware:
    """Tests for TrustedHostMiddleware."""

    def test_import(self):
        """TrustedHostMiddleware is importable."""
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        assert TrustedHostMiddleware is not None

    def test_allow_all(self):
        """Wildcard allows all hosts."""
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        mw = TrustedHostMiddleware(allowed_hosts=["*"])
        assert mw.is_valid_host("example.com") is True
        assert mw.is_valid_host("anything.org") is True

    def test_default_allows_all(self):
        """Default configuration allows all hosts."""
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        mw = TrustedHostMiddleware()
        assert mw.is_valid_host("example.com") is True

    def test_specific_host(self):
        """Only the specified host is allowed."""
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        mw = TrustedHostMiddleware(allowed_hosts=["example.com"])
        assert mw.is_valid_host("example.com") is True
        assert mw.is_valid_host("evil.com") is False

    def test_host_with_port(self):
        """Port is stripped before matching."""
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        mw = TrustedHostMiddleware(allowed_hosts=["example.com"])
        assert mw.is_valid_host("example.com:8000") is True
        assert mw.is_valid_host("evil.com:8000") is False

    def test_wildcard_subdomain(self):
        """Wildcard subdomain pattern matches."""
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        mw = TrustedHostMiddleware(allowed_hosts=["*.example.com"])
        assert mw.is_valid_host("api.example.com") is True
        assert mw.is_valid_host("deep.sub.example.com") is True
        assert mw.is_valid_host("example.com") is False
        assert mw.is_valid_host("evil.com") is False

    def test_case_insensitive(self):
        """Host matching is case-insensitive."""
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        mw = TrustedHostMiddleware(allowed_hosts=["Example.COM"])
        assert mw.is_valid_host("example.com") is True
        assert mw.is_valid_host("EXAMPLE.COM") is True

    def test_middleware_type_attribute(self):
        """Has _fastapi_turbo_middleware_type attribute."""
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        assert TrustedHostMiddleware._fastapi_turbo_middleware_type == "trustedhost"

    def test_multiple_allowed_hosts(self):
        """Multiple hosts can be specified."""
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        mw = TrustedHostMiddleware(allowed_hosts=["a.com", "b.com"])
        assert mw.is_valid_host("a.com") is True
        assert mw.is_valid_host("b.com") is True
        assert mw.is_valid_host("c.com") is False


# ===========================================================================
# 8. HTTPSRedirectMiddleware
# ===========================================================================


class TestHTTPSRedirectMiddleware:
    """Tests for HTTPSRedirectMiddleware."""

    def test_import(self):
        """HTTPSRedirectMiddleware is importable."""
        from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
        assert HTTPSRedirectMiddleware is not None

    def test_redirect_url_http(self):
        """HTTP URLs are redirected to HTTPS."""
        from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
        url = HTTPSRedirectMiddleware.redirect_url("http://example.com/path")
        assert url == "https://example.com/path"

    def test_redirect_url_https_unchanged(self):
        """HTTPS URLs are returned unchanged."""
        from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
        url = HTTPSRedirectMiddleware.redirect_url("https://example.com/path")
        assert url == "https://example.com/path"

    def test_should_redirect_http(self):
        """HTTP scheme triggers redirect."""
        from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
        assert HTTPSRedirectMiddleware.should_redirect("http") is True

    def test_should_redirect_https(self):
        """HTTPS scheme does not trigger redirect."""
        from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
        assert HTTPSRedirectMiddleware.should_redirect("https") is False

    def test_should_redirect_wss(self):
        """WSS scheme does not trigger redirect."""
        from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
        assert HTTPSRedirectMiddleware.should_redirect("wss") is False

    def test_middleware_type_attribute(self):
        """Has _fastapi_turbo_middleware_type attribute."""
        from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
        assert HTTPSRedirectMiddleware._fastapi_turbo_middleware_type == "httpsredirect"

    def test_redirect_url_with_port(self):
        """HTTP URL with port is correctly redirected."""
        from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
        url = HTTPSRedirectMiddleware.redirect_url("http://example.com:8080/path")
        assert url == "https://example.com:8080/path"

    def test_redirect_url_with_query(self):
        """HTTP URL with query string is correctly redirected."""
        from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
        url = HTTPSRedirectMiddleware.redirect_url("http://example.com/path?q=1")
        assert url == "https://example.com/path?q=1"
