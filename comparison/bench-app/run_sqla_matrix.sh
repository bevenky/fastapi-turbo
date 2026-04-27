#!/usr/bin/env bash
# SQLA matrix — runs each (mode × stack) sequentially to avoid exhausting
# Postgres max_connections (six pools × 15 conns = 90 vs default 100).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BENCH="$PROJECT_ROOT/target/release/fastapi-turbo-bench"
PY_RS="python3"
PY_FA="$PROJECT_ROOT/comparison/fastapi-venv/bin/python"

N=3000
WARMUP=200

CHILD=""
cleanup() {
    [ -n "$CHILD" ] || return 0
    kill -TERM "$CHILD" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        kill -0 "$CHILD" 2>/dev/null || { CHILD=""; return 0; }
        sleep 1
    done
    kill -KILL "$CHILD" 2>/dev/null || true
    CHILD=""
}
trap cleanup EXIT INT TERM HUP

wait_port() {
    local port=$1
    for _ in $(seq 1 30); do
        curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
        sleep 0.4
    done
    return 1
}

seed() {
    local port=$1
    curl -sf -X POST "http://127.0.0.1:$port/users" \
        -H "Content-Type: application/json" \
        -d '{"email":"bench@example.com","name":"Bench User"}' >/dev/null 2>&1 || true
}

bench_one() {
    local label=$1 port=$2 path=$3
    local out="$($BENCH 127.0.0.1 "$port" "$path" "$N" "$WARMUP" 2>&1)"
    local rps=$(echo "$out" | grep -oE '[0-9]+ req/s' | head -1 | cut -d' ' -f1)
    local p50=$(echo "$out" | grep -oE 'p50=[0-9]+' | head -1 | cut -d= -f2)
    local p99=$(echo "$out" | grep -oE 'p99=[0-9]+' | head -1 | cut -d= -f2)
    printf "%s\t%s\t%s\t%s\t%s\n" "$label" "$path" "${rps:-?}" "${p50:-?}" "${p99:-?}"
}

run_one() {
    local label=$1 mode=$2 stack=$3 port=$4
    local py=$PY_RS
    [ "$stack" = "uvicorn" ] && py=$PY_FA
    cd "$PROJECT_ROOT"
    $py "$SCRIPT_DIR/sqla_runner.py" "$mode" "$stack" "$port" >/tmp/sqla_${port}.log 2>&1 &
    CHILD=$!
    if ! wait_port "$port"; then
        echo "$label\tFAIL_START\t?\t?\t?" >&2
        cleanup
        return 1
    fi
    seed "$port"
    bench_one "$label" "$port" "/health"
    bench_one "$label" "$port" "/users/1"
    cleanup
    sleep 0.5  # let PG reclaim connections
}

echo -e "label\tendpoint\trps\tp50\tp99"
# Track per-row exit status. Earlier runs swallowed every failure
# with ``|| true``, so a missing pg2 driver or a timeout silently
# produced a TSV with NO row for that combination — and downstream
# tables in latest_bench.md kept the stale numbers from the previous
# run. Now we record failures as TSV rows with ``ERR`` in place of
# numbers and exit non-zero at the end so CI catches the gap. Set
# ``SQLA_BENCH_ALLOW_FAILURES=1`` to force soft-failure mode (e.g.
# when running against a pg2-less environment intentionally).
FAILED=()
run_or_record() {
    local label="$1"; shift
    local args=("$@")
    if ! run_one "$label" "${args[@]}"; then
        printf "%s\t%s\t%s\t%s\t%s\n" "$label" "ERR" "0" "0" "0"
        FAILED+=("$label")
    fi
}
run_or_record "fastapi-turbo_SQLA_pg3-sync"  pg3       fastapi-turbo 19050
run_or_record "fastapi-turbo_SQLA_pg2-sync"  pg2       fastapi-turbo 19051
run_or_record "fastapi-turbo_SQLA_asyncpg"   async     fastapi-turbo 19052
run_or_record "fastapi-turbo_SQLA_pg3-async" pg3async  fastapi-turbo 19056
run_or_record "FastAPI_SQLA_pg3-sync"     pg3       uvicorn    19053
run_or_record "FastAPI_SQLA_pg2-sync"     pg2       uvicorn    19054
run_or_record "FastAPI_SQLA_asyncpg"      async     uvicorn    19055
run_or_record "FastAPI_SQLA_pg3-async"    pg3async  uvicorn    19057
if [ ${#FAILED[@]} -gt 0 ] && [ "${SQLA_BENCH_ALLOW_FAILURES:-}" != "1" ]; then
    echo "SQLA matrix: ${#FAILED[@]} row(s) failed: ${FAILED[*]}" >&2
    exit 1
fi
