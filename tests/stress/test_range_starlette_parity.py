"""Direct parity matrix: same Range header → same status code in
upstream Starlette FileResponse and fastapi_turbo's FileResponse.

Drives both stacks through ``httpx.ASGITransport`` and asserts
identical status codes (and identical bodies for the success cases).

The pre-fix turbo parser was lenient (treating malformed headers as
200 full body); upstream is strict (400 on malformed, 416 on
out-of-bounds). This test locks the contract.

Uses the same sys.modules swap pattern as
``test_asgi_in_process_parity_contract.py`` so we can import the
REAL upstream Starlette in the same test process where
``fastapi_turbo``'s shim is otherwise active."""
import asyncio
import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_turbo_shim_after_each_test():
    """Swap the upstream import in, then put the turbo shim back so
    later test files in the run see our classes via
    ``from fastapi import …``."""
    yield
    _drop_fa_modules()
    from fastapi_turbo.compat import install as _in, uninstall as _un
    _un()
    importlib.invalidate_caches()
    _in()


def _drop_fa_modules():
    for m in list(sys.modules):
        if (
            m == "fastapi"
            or m.startswith("fastapi.")
            or m == "starlette"
            or m.startswith("starlette.")
        ):
            del sys.modules[m]


def _import_upstream():
    from fastapi_turbo.compat import uninstall as _un
    _un()
    _drop_fa_modules()
    importlib.invalidate_caches()
    from starlette.applications import Starlette  # noqa: F401
    from starlette.responses import FileResponse  # noqa: F401
    from starlette.routing import Route  # noqa: F401
    return (
        sys.modules["starlette.applications"],
        sys.modules["starlette.responses"],
        sys.modules["starlette.routing"],
    )


def _import_turbo():
    from fastapi_turbo.compat import install as _in, uninstall as _un
    _drop_fa_modules()
    _un()
    importlib.invalidate_caches()
    _in()
    return sys.modules["fastapi"], sys.modules["fastapi.responses"]


def _run(coro):
    return asyncio.run(coro)


def _build_starlette_app(path):
    apps_mod, resp_mod, routing_mod = _import_upstream()
    Starlette = apps_mod.Starlette
    StarFR = resp_mod.FileResponse
    Route = routing_mod.Route

    async def serve(_request):
        return StarFR(str(path))

    return Starlette(routes=[Route("/f", serve)])


def _build_turbo_app(path):
    fastapi_mod, resp_mod = _import_turbo()
    FastAPI = fastapi_mod.FastAPI
    TurboFR = resp_mod.FileResponse

    app = FastAPI()

    @app.get("/f")
    def _f():
        return TurboFR(str(path))

    return app


async def _hit(app, range_value):
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://t",
    ) as cli:
        return await cli.get("/f", headers={"Range": range_value})


PARITY_CASES = [
    ("bytes=0-2",        206, "trivial single range"),
    ("bytes=0-",         206, "open-ended"),
    ("bytes=-3",         206, "suffix"),
    ("bytes=-0",         416, "zero-length suffix → unsatisfiable"),
    ("bytes=10-1",       400, "reversed → malformed"),
    ("bytes=abc-def",    400, "non-numeric → malformed"),
    ("items=0-5",        400, "wrong unit → malformed"),
    ("Bytes=0-1",        206, "case-insensitive unit accepted"),
    ("bytes=0-19,0-19",  206, "duplicate sub-range coalesces to single"),
    ("bytes=0-9,10-19",  206, "adjacent sub-ranges coalesce to single"),
    ("bytes=0-9,5-14",   206, "overlapping sub-ranges coalesce to single"),
    ("bytes=0-3,15-19",  206, "disjoint → multipart"),
    ("bytes=0-4,100-200",416, "any sub-range past EOF → unsatisfiable"),
    ("bytes=100-200",    416, "single sub-range past EOF → unsatisfiable"),
    ("bytes=",           400, "empty range value → malformed"),
    ("garbage",          400, "no equals sign → malformed"),
]


def _make_payload_file(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"0123456789abcdefghij")  # 20 bytes
    return f


def test_range_status_code_parity(tmp_path):
    """Status code matches upstream for every case in the matrix."""
    f = _make_payload_file(tmp_path)
    star_app = _build_starlette_app(f)
    turbo_app = _build_turbo_app(f)

    async def go():
        for range_value, expected, comment in PARITY_CASES:
            star_r = await _hit(star_app, range_value)
            turbo_r = await _hit(turbo_app, range_value)
            assert star_r.status_code == expected, (
                f"upstream Starlette returned unexpected status for {range_value!r} "
                f"({comment}): got {star_r.status_code}, test expected {expected}"
            )
            assert turbo_r.status_code == star_r.status_code, (
                f"parity mismatch for {range_value!r} ({comment}): "
                f"starlette={star_r.status_code} turbo={turbo_r.status_code}"
            )

    _run(go())


def test_range_body_parity_for_success_cases(tmp_path):
    """For 206 cases the BODY must also match upstream byte-for-byte
    (excluding the multipart boundary which is randomized)."""
    f = _make_payload_file(tmp_path)
    star_app = _build_starlette_app(f)
    turbo_app = _build_turbo_app(f)

    SUCCESS_CASES = [
        ("bytes=0-2", b"012"),
        ("bytes=-3", b"hij"),
        ("bytes=0-", b"0123456789abcdefghij"),
        ("Bytes=0-1", b"01"),
        ("bytes=0-19,0-19", b"0123456789abcdefghij"),
        ("bytes=0-9,10-19", b"0123456789abcdefghij"),
    ]

    async def go():
        for range_value, expected_body in SUCCESS_CASES:
            star_r = await _hit(star_app, range_value)
            turbo_r = await _hit(turbo_app, range_value)
            assert star_r.status_code == 206
            assert turbo_r.status_code == 206
            assert star_r.content == expected_body, (
                f"upstream body mismatch for {range_value!r}: "
                f"got {star_r.content!r} expected {expected_body!r}"
            )
            assert turbo_r.content == expected_body, (
                f"turbo body mismatch for {range_value!r}: "
                f"got {turbo_r.content!r} expected {expected_body!r}"
            )

    _run(go())


def test_400_body_parity(tmp_path):
    """For 400 cases the response body string must match upstream
    exactly — many clients surface the body verbatim in dev tools /
    logs, and copy-paste between Starlette- and turbo-served apps
    breaks if the strings differ."""
    f = _make_payload_file(tmp_path)
    star_app = _build_starlette_app(f)
    turbo_app = _build_turbo_app(f)

    # (range_value, expected_body_bytes)
    BODY_CASES = [
        ("items=0-5",     b"Only support bytes range"),
        ("garbage",       b"Malformed range header."),
        ("bytes=10-1",    b"Range header: start must be less than end"),
        ("bytes=abc-def", b"Range header: range must be requested"),
        ("bytes=",        b"Range header: range must be requested"),
    ]

    async def go():
        for range_value, expected in BODY_CASES:
            star_r = await _hit(star_app, range_value)
            turbo_r = await _hit(turbo_app, range_value)
            assert star_r.status_code == 400
            assert turbo_r.status_code == 400
            assert star_r.content == expected, (
                f"upstream body unexpected for {range_value!r}: "
                f"got {star_r.content!r}"
            )
            assert turbo_r.content == expected, (
                f"turbo body diverges for {range_value!r}: "
                f"got {turbo_r.content!r} expected {expected!r}"
            )
            # Error-body Content-Type must be text/plain; charset=utf-8.
            assert turbo_r.headers["content-type"] == "text/plain; charset=utf-8"
            assert turbo_r.headers["content-length"] == str(len(expected))

    _run(go())


def test_416_header_shape_parity(tmp_path):
    """For 416 cases the response must carry exactly the upstream
    headers: Content-Range, Content-Length: 0, Content-Type:
    text/plain; charset=utf-8. NO accept-ranges, NO last-modified,
    NO etag — Starlette treats 416 as a generic PlainTextResponse
    rather than an entity response, so the validators are absent."""
    f = _make_payload_file(tmp_path)  # 20 bytes
    star_app = _build_starlette_app(f)
    turbo_app = _build_turbo_app(f)

    async def go():
        for range_value in ["bytes=-0", "bytes=100-200", "bytes=0-4,100-200"]:
            star_r = await _hit(star_app, range_value)
            turbo_r = await _hit(turbo_app, range_value)
            assert star_r.status_code == 416
            assert turbo_r.status_code == 416

            # Required headers.
            assert turbo_r.headers["content-range"] == "bytes */20"
            assert turbo_r.headers["content-length"] == "0"
            assert turbo_r.headers["content-type"] == "text/plain; charset=utf-8"

            # Forbidden headers (absent in upstream 416).
            for forbidden in ("accept-ranges", "last-modified", "etag"):
                assert forbidden not in turbo_r.headers, (
                    f"turbo 416 leaks {forbidden!r}: {dict(turbo_r.headers)}"
                )
                assert forbidden not in star_r.headers, (
                    f"upstream regression: 416 contains {forbidden!r}"
                )

            assert turbo_r.content == b""

    _run(go())


def test_multipart_wire_format_parity(tmp_path):
    """Wire framing must match upstream: CRLF separators, leading
    ``--{boundary}`` (no CRLF prefix), per-part Content-Type echoes
    the response Content-Type with ``; charset=utf-8`` for textual
    files, closing ``--{boundary}--`` with no trailing CRLF."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"0123456789abcdefghij")
    star_app = _build_starlette_app(f)
    turbo_app = _build_turbo_app(f)

    async def go():
        rng = "bytes=0-3,15-19"
        star_r = await _hit(star_app, rng)
        turbo_r = await _hit(turbo_app, rng)
        assert star_r.status_code == 206
        assert turbo_r.status_code == 206

        for label, r in [("starlette", star_r), ("turbo", turbo_r)]:
            ct = r.headers["content-type"]
            assert ct.startswith("multipart/byteranges; boundary="), (label, ct)
            boundary = ct.split("boundary=", 1)[1]
            body = r.content

            assert b"\r\n" in body, f"{label}: missing CRLF framing"
            assert not body.startswith(b"\r\n"), (
                f"{label}: body starts with leading CRLF: {body[:20]!r}"
            )
            assert b"Content-Type: text/plain; charset=utf-8" in body, (
                f"{label}: per-part Content-Type missing/wrong: "
                f"{body[:300]!r}"
            )
            assert b"Content-Range: bytes 0-3/20" in body, label
            assert b"Content-Range: bytes 15-19/20" in body, label
            closing = f"--{boundary}--".encode()
            assert closing in body, f"{label}: closing boundary missing"
            assert body.endswith(closing), (
                f"{label}: body should end with closing boundary; "
                f"trailing bytes: {body[-30:]!r}"
            )

    _run(go())
