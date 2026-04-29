"""R27 audit follow-ups — Rust router 405 Allow first-match parity,
CI workflow injects shim conftests + makes external gates blocking,
benchmark TSVs / shell runners stop swallowing failures, README /
COMPATIBILITY claims aligned with measured reality."""
import pathlib

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 Rust router 405 Allow header — first-match-wins parity
# ────────────────────────────────────────────────────────────────────
#
# These exercise the REAL loopback Rust path (no ``in_process=True``)
# — the bug the audit caught was that the Rust matcher's
# most-specific-literal selection leaked into the 405 Allow header.
# R27 post-processes the per-path Allow value via a registration-
# order pattern walk so the Rust path now matches Starlette's
# first-match-wins behaviour.

pytestmark_real_loopback = pytest.mark.requires_loopback


@pytest.mark.requires_loopback
def test_rust_options_uses_first_matching_route_allow():
    """OPTIONS /items/special on the Rust server: registered route
    ``/items/{id}`` (GET) wins over ``/items/special`` (POST) by
    registration order. Was a documented Different-by-design
    divergence; R27 makes it Full parity."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/items/{id}")
    def _g(id: int):
        return {}

    @app.post("/items/special")
    def _p():
        return {}

    with TestClient(app) as c:  # default = real loopback
        r = c.request("OPTIONS", "/items/special")
        assert r.status_code == 405
        assert r.headers["allow"] == "GET", r.headers["allow"]


@pytest.mark.requires_loopback
def test_rust_options_three_way_overlap_first_match_wins():
    """Three-way overlap (``/a/{x}/{y}`` GET, ``/a/{x}/lit`` POST,
    ``/a/lit/lit`` PUT) — OPTIONS /a/lit/lit must report ``Allow:
    GET`` because ``/a/{x}/{y}`` was registered first."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/a/{x}/{y}")
    def _a(x: str, y: str):
        return {}

    @app.post("/a/{x}/lit")
    def _b(x: str):
        return {}

    @app.put("/a/lit/lit")
    def _c():
        return {}

    with TestClient(app) as c:
        r = c.request("OPTIONS", "/a/lit/lit")
        assert r.status_code == 405
        assert r.headers["allow"] == "GET", r.headers["allow"]


# ────────────────────────────────────────────────────────────────────
# #2 CI workflow: external gates inject shim + are blocking
# ────────────────────────────────────────────────────────────────────


def test_ci_workflow_injects_shim_conftest_for_external_suites():
    """Earlier CI ran ``python -c 'import fastapi_turbo'`` in a
    separate process — the shim wasn't installed inside the pytest
    process that ran the upstream tests. Real CI runs against fresh
    clones (no pre-existing shim conftest), so the gate was a false
    positive: it tested upstream FastAPI / Sentry against itself.
    R27 writes a per-tree conftest that imports fastapi_turbo at
    session-start so the shim is live during collection + teardown.
    R51 moved the conftest-injection logic into the canonical
    ``scripts/run_external_compat_gates.sh``; this test now
    enforces it on the script (single source of truth)."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    ci_text = (repo / ".github" / "workflows" / "ci.yml").read_text()
    script_text = (
        repo / "scripts" / "run_external_compat_gates.sh"
    ).read_text()
    # CI invokes the canonical script.
    assert "scripts/run_external_compat_gates.sh" in ci_text, ci_text
    # Canonical script writes the shim conftest for the upstream tree.
    assert "/tmp/fastapi_upstream/conftest.py" in script_text, script_text
    # And for both Sentry integration trees (fastapi + asgi).
    assert "for tree in fastapi asgi" in script_text or (
        "/tmp/sentry-python/tests/integrations/fastapi/conftest.py" in script_text
        and "/tmp/sentry-python/tests/integrations/asgi/conftest.py" in script_text
    ), script_text
    # Each shim conftest imports fastapi_turbo at session-start.
    assert "import fastapi_turbo" in script_text, script_text


def test_ci_workflow_external_gates_are_blocking():
    """The previous workflow used ``|| echo "..."`` so a failing
    upstream / Sentry run printed a warning but the job still
    succeeded. R27 removes the soft-fail and pins the external repos.
    R28 changes the pinning mechanism from ``git clone --branch <tag>``
    to ``git fetch + reset --hard <tag>`` so reused runners don't
    silently inherit a stale checkout. R51 consolidates both gates
    into the canonical ``scripts/run_external_compat_gates.sh`` so
    PR CI / release CI / local audit run identical semantics —
    this test now enforces "the workflow MUST invoke the script
    AND the script MUST keep the pinning + reset-hard contract"."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    ci_text = (repo / ".github" / "workflows" / "ci.yml").read_text()
    script_text = (repo / "scripts" / "run_external_compat_gates.sh").read_text()

    # 1. CI must invoke the canonical script (no inline drift).
    assert "scripts/run_external_compat_gates.sh" in ci_text, ci_text
    # 2. No soft-fail anywhere in the CI compat-gate region.
    assert "|| echo" not in ci_text, ci_text
    # 3. The canonical script must keep the pinning + force-reset
    #    contract for both external trees.
    assert 'UPSTREAM_TAG="0.136.0"' in script_text, script_text
    assert 'SENTRY_TAG="2.42.0"' in script_text, script_text
    assert "git -C /tmp/fastapi_upstream" in script_text and "reset --hard" in script_text, (
        "canonical script must force-reset /tmp/fastapi_upstream to the pin"
    )
    assert "git -C /tmp/sentry-python" in script_text and "reset --hard" in script_text, (
        "canonical script must force-reset /tmp/sentry-python to the pin"
    )


# ────────────────────────────────────────────────────────────────────
# #3 Benchmark artifacts: TSV header + runner soft-fail removal
# ────────────────────────────────────────────────────────────────────


def test_v3_tsv_starts_with_header_not_cargo_output():
    """``benchmarks/v3.tsv`` first line must be the column header,
    not ``Finished `release` profile ...`` from cargo build output.
    R27 redirects cargo / npm noise to stderr so it doesn't leak
    into the captured TSV."""
    tsv = pathlib.Path(__file__).resolve().parents[2] / "benchmarks" / "v3.tsv"
    first = tsv.read_text().splitlines()[0]
    assert first.startswith("framework\t"), first


def test_run_sqla_matrix_doesnt_swallow_failures():
    """The earlier ``run_sqla_matrix.sh`` ended every row with
    ``|| true`` so a failed driver produced NO TSV row, and
    ``latest_bench.md`` happily rendered stale numbers from a
    previous run. R27 records failed rows as ``ERR`` and exits
    non-zero; ``SQLA_BENCH_ALLOW_FAILURES=1`` is the explicit
    soft-fail opt-out for envs missing optional drivers."""
    runner = pathlib.Path(__file__).resolve().parents[2] / "comparison" / "bench-app" / "run_sqla_matrix.sh"
    text = runner.read_text()
    # The bare ``|| true`` shouldn't appear on the run_one rows.
    for line in text.splitlines():
        if "run_one " in line and not line.lstrip().startswith("#"):
            assert "|| true" not in line, line
    # Non-zero exit when failures accumulate must be present.
    assert "exit 1" in text, "matrix runner must fail when rows fail"


def test_run_benchmark_v3_redirects_build_noise_to_stderr():
    """``run_benchmark_v3.sh`` must emit cargo / npm build output
    on stderr only — earlier the ``cargo build --release 2>&1 |
    tail -1`` line wrote ``Finished ...`` to stdout, which polluted
    the TSV when the runner was captured via ``> v3.tsv``."""
    runner = pathlib.Path(__file__).resolve().parents[2] / "comparison" / "bench-app" / "run_benchmark_v3.sh"
    text = runner.read_text()
    # Build noise must be inside a stderr-redirect block.
    assert "} 1>&2" in text, "build noise must redirect to stderr"


# ────────────────────────────────────────────────────────────────────
# #4 COMPATIBILITY.md status reconciled with reality
# ────────────────────────────────────────────────────────────────────


def test_compatibility_doc_no_longer_lists_stale_sentry_count():
    """COMPATIBILITY.md previously claimed Sentry FastAPI integration
    54/56. The actual shim-injected run is 89/89 (the 54/56 number
    pre-dated R23/R25/R26). The doc must reflect the true count."""
    compat = pathlib.Path(__file__).resolve().parents[2] / "COMPATIBILITY.md"
    text = compat.read_text()
    assert "54/56" not in text, text
    assert "89/89" in text, text


def test_compatibility_doc_no_longer_calls_active_thread_profiling_unwired():
    """COMPATIBILITY.md previously listed active-thread-id profiling
    under ``SentryAsgiMiddleware(app)`` as ``Not wired``. The Sentry
    upstream ``test_active_thread_id`` cases now pass under the shim;
    the limitation entry must be removed and the table row marked
    ``Full``."""
    compat = pathlib.Path(__file__).resolve().parents[2] / "COMPATIBILITY.md"
    text = compat.read_text()
    assert "Not wired" not in text, text


def test_readme_doesnt_promise_unpublished_linux_ci_numbers():
    """README previously said ``CI publishes a Linux x86_64 run per
    release`` even though benchmarks.md said Linux numbers are TODO.
    R27 brings the README in line: it now explicitly says Linux
    numbers are NOT yet published."""
    readme = pathlib.Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text()
    assert "CI publishes a Linux x86_64 run per release" not in text, text
    assert "not yet published" in text, text


def test_readme_db_table_isnt_malformed():
    """The DB performance table previously had rows with mismatched
    column counts (``| Winner |`` column on some rows but not
    others). R26 dropped the Winner column; R27 normalises the
    remaining sequential-query rows to the same 3-column shape."""
    readme = pathlib.Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text()
    # Find the DB perf table: starts with ``| Queries |`` header.
    lines = text.splitlines()
    in_table = False
    table_lines: list[str] = []
    for ln in lines:
        if ln.startswith("| Queries "):
            in_table = True
        if in_table:
            if not ln.startswith("|"):
                break
            table_lines.append(ln)
    assert table_lines, "DB perf table not found"
    # All rows must have the same number of pipe separators.
    sep_counts = {ln.count("|") for ln in table_lines}
    assert len(sep_counts) == 1, (sep_counts, table_lines)
