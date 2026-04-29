"""R28 audit follow-ups — ``app.host()`` dispatches under
``ASGITransport`` without binding a socket, CI external gates
force-reset to pinned tags every run + cover the ASGI integration
tree, and ``benchmarks/latest_bench.md`` SQLA section reflects the
authoritative TSV."""
import asyncio
import csv
import pathlib

import httpx
import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 app.host() works under ASGITransport — no loopback bind
# ────────────────────────────────────────────────────────────────────


def test_app_host_dispatches_via_asgi_transport_without_socket():
    """``httpx.ASGITransport(app=app)`` against an app that uses
    ``app.host("subapp", subapp)`` must route to the sub-app
    WITHOUT spinning up a loopback Rust server. Probe-confirmed
    failure path (R28): the dispatcher fell through to
    ``_asgi_ensure_server`` → ``socket.bind("127.0.0.1", 0)`` →
    ``PermissionError`` on serverless / sandbox runs.

    Now the in-process dispatcher checks ``_hosts`` BEFORE route
    match and recurses into the sub-app's ``__call__`` directly."""
    from fastapi_turbo import FastAPI

    main = FastAPI()
    sub = FastAPI()

    @sub.get("/info")
    async def info():
        return {"where": "subapp"}

    @main.get("/main")
    async def m():
        return {"where": "main"}

    main.host("subapp", sub)

    async def _drive() -> tuple[int, dict, int, dict]:
        transport = httpx.ASGITransport(app=main)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://example.com"
        ) as c:
            r = await c.get("/info", headers={"Host": "subapp"})
            r2 = await c.get("/main", headers={"Host": "example.com"})
        return r.status_code, r.json(), r2.status_code, r2.json()

    sub_status, sub_body, main_status, main_body = asyncio.run(_drive())
    assert sub_status == 200, sub_status
    assert sub_body == {"where": "subapp"}, sub_body
    assert main_status == 200, main_status
    assert main_body == {"where": "main"}, main_body


def test_app_host_supports_label_match_subapp_dot_domain():
    """``app.host("subapp", sub)`` must match BOTH ``Host: subapp``
    and ``Host: subapp.example.com`` (Starlette's leading-label
    semantics for hostnames without a dot)."""
    from fastapi_turbo import FastAPI

    main = FastAPI()
    sub = FastAPI()

    @sub.get("/x")
    async def _x():
        return {"sub": True}

    main.host("subapp", sub)

    async def _drive() -> int:
        transport = httpx.ASGITransport(app=main)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://example.com"
        ) as c:
            r = await c.get("/x", headers={"Host": "subapp.example.com"})
        return r.status_code

    assert asyncio.run(_drive()) == 200


# ────────────────────────────────────────────────────────────────────
# #2 CI external gates: force-reset + ASGI tree gated
# ────────────────────────────────────────────────────────────────────


def test_ci_workflow_force_resets_external_pins():
    """Earlier CI did ``[ ! -d /tmp/sentry-python ] && git clone``
    — on a reused runner with a stale checkout (e.g. 2.58.0 instead
    of the pinned 2.42.0), the gate silently tested the wrong
    version. R28 force-fetches and resets to the pin every run.
    R51 consolidates the logic into ``scripts/run_external_compat_
    gates.sh``; the contract is enforced on the script now."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    ci_text = (repo / ".github" / "workflows" / "ci.yml").read_text()
    script_text = (
        repo / "scripts" / "run_external_compat_gates.sh"
    ).read_text()
    # CI invokes the canonical script (single source of truth).
    assert "scripts/run_external_compat_gates.sh" in ci_text, ci_text
    # Both external trees must hard-reset to the pin, never lazy-
    # skip the clone via ``[ ! -d <tree> ]``.
    for tree in ("/tmp/fastapi_upstream", "/tmp/sentry-python"):
        assert f"git -C {tree}" in script_text, tree
        assert "reset --hard" in script_text, tree
        assert f"[ ! -d {tree} ]" not in script_text, tree


def test_ci_workflow_runs_sentry_asgi_integration_tree_too():
    """COMPATIBILITY.md claims Sentry ASGI 33/33 + FastAPI 89/89.
    The earlier CI workflow only ran ``tests/integrations/fastapi``
    so the ASGI claim was never enforced. R28 gates BOTH trees;
    R51 moved the invocation into the canonical script — this
    test enforces that the script runs both."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script_text = (
        repo / "scripts" / "run_external_compat_gates.sh"
    ).read_text()
    assert "/tmp/sentry-python/tests/integrations/fastapi" in script_text, script_text
    assert "/tmp/sentry-python/tests/integrations/asgi" in script_text, script_text


# ────────────────────────────────────────────────────────────────────
# #3 latest_bench.md SQLA table matches sqla.tsv
# ────────────────────────────────────────────────────────────────────


def _load_sqla_tsv() -> dict[tuple[str, str], dict[str, int]]:
    """Return ``{(label, endpoint): {rps, p50, p99}}``."""
    path = pathlib.Path(__file__).resolve().parents[2] / "benchmarks" / "sqla.tsv"
    out: dict[tuple[str, str], dict[str, int]] = {}
    with path.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            out[(row["label"], row["endpoint"])] = {
                "rps": int(row["rps"]),
                "p50": int(row["p50"]),
                "p99": int(row["p99"]),
            }
    return out


def test_latest_bench_sqla_table_matches_tsv_for_users_endpoint():
    """The SQLA section in ``benchmarks/latest_bench.md`` must
    render the exact rps numbers from ``benchmarks/sqla.tsv`` for
    every ``/users/1`` row (the audit caught a 3 638 vs 3 982
    drift). All four fastapi-turbo stacks AND all four FastAPI
    stacks must appear."""
    tsv = _load_sqla_tsv()
    md = (
        pathlib.Path(__file__).resolve().parents[2] / "benchmarks" / "latest_bench.md"
    ).read_text()

    # For each fastapi-turbo and FastAPI stack, the /users/1 rps from
    # the TSV must appear in the rendered table (formatted with a
    # space thousands separator: ``3 982``).
    for label_prefix in (
        "fastapi-turbo_SQLA_pg3-sync",
        "fastapi-turbo_SQLA_pg2-sync",
        "fastapi-turbo_SQLA_asyncpg",
        "fastapi-turbo_SQLA_pg3-async",
        "FastAPI_SQLA_pg3-sync",
        "FastAPI_SQLA_pg2-sync",
        "FastAPI_SQLA_asyncpg",
        "FastAPI_SQLA_pg3-async",
    ):
        rps = tsv[(label_prefix, "/users/1")]["rps"]
        # Render rps with a space thousands separator (matches the
        # table formatting in latest_bench.md).
        rendered = f"{rps:,}".replace(",", " ")
        assert rendered in md, (label_prefix, rps, rendered)


def test_latest_bench_includes_pg3_async_rows():
    """The TSV has both ``pg3-async`` rows (fastapi-turbo and
    FastAPI). Earlier the Markdown table omitted them — drift
    against the TSV. R28 brings the rendered table back in line."""
    md = (
        pathlib.Path(__file__).resolve().parents[2] / "benchmarks" / "latest_bench.md"
    ).read_text()
    assert "psycopg3 (async)" in md, md
