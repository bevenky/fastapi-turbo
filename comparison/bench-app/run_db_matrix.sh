#!/usr/bin/env bash
# DB driver matrix benchmark: spin up one app per (framework × driver × mode),
# run identical workload through the Rust bench client, emit TSV.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BENCH="$PROJECT_ROOT/target/release/fastapi-rs-bench"

PY_RS="python3"
PY_FA="$PROJECT_ROOT/comparison/fastapi-venv/bin/python"

N=5000
WARMUP=200

PIDS=()
trap 'for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; wait "$p" 2>/dev/null || true; done' EXIT

wait_port() {
    local port=$1
    for _ in $(seq 1 20); do
        curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
        sleep 0.5
    done
    return 1
}

start_rs_app() {
    local app=$1 port=$2
    FASTAPI_RS_NO_SHIM=1 PORT=$port $PY_RS "$SCRIPT_DIR/$app" >/tmp/bench_${port}.log 2>&1 &
    PIDS+=($!)
    wait_port $port || { echo "Failed $app:$port" >&2; tail -5 /tmp/bench_${port}.log >&2; return 1; }
}

start_fa_app() {
    local app=$1 port=$2
    PORT=$port $PY_FA "$SCRIPT_DIR/$app" >/tmp/bench_${port}.log 2>&1 &
    PIDS+=($!)
    wait_port $port || { echo "Failed $app:$port" >&2; tail -5 /tmp/bench_${port}.log >&2; return 1; }
}

start_gogin() {
    local port=$1
    PORT=$port "$SCRIPT_DIR/db-gin" >/tmp/bench_${port}.log 2>&1 &
    PIDS+=($!)
    wait_port $port || return 1
}

bench_one() {
    local label=$1 port=$2 path=$3 method=${4:-GET} body=${5:-}
    local out
    if [ "$method" = "GET" ]; then
        out="$($BENCH 127.0.0.1 "$port" "$path" "$N" "$WARMUP" 2>&1)"
    else
        out="$($BENCH 127.0.0.1 "$port" "$path" "$N" "$WARMUP" "$method" "$body" "application/json" 2>&1)"
    fi
    local rps=$(echo "$out" | grep -oE '[0-9]+ req/s' | head -1 | cut -d' ' -f1)
    local p50=$(echo "$out" | grep -oE 'p50=[0-9]+' | head -1 | cut -d= -f2)
    local p99=$(echo "$out" | grep -oE 'p99=[0-9]+' | head -1 | cut -d= -f2)
    printf "%s\t%s\t%s\t%s\t%s\n" "$label" "$path ($method)" "${rps:-?}" "${p50:-?}" "${p99:-?}"
}

# Build go-gin if missing
[ -x "$SCRIPT_DIR/db-gin" ] || (cd "$SCRIPT_DIR" && go build -o db-gin db_go_gin.go)

# ── Start apps on distinct ports ────────────────────────────────────────
start_rs_app "db_fastapi_rs_app.py"        19030 &
start_rs_app "db_async_psycopg3_app.py"    19032 &
start_rs_app "db_sync_fastapi_rs_app.py"   19033 &
start_fa_app "db_fastapi_uvicorn_app.py"   19034 &
start_gogin 19031 &
wait

CREATE_BODY='{"name":"Bench","price":42.99,"category_id":1,"stock":10}'

echo -e "label\tendpoint\trps\tp50\tp99"

# Mapping: label → port
declare -A APPS=(
    [fastapi-rs_pg3_sync]=19030
    [Go-Gin]=19031
    [fastapi-rs_pg3_async]=19032
    [fastapi-rs_pg2_sync]=19033
    [FastAPI_asyncpg]=19034
)

for label in "fastapi-rs_pg3_sync" "fastapi-rs_pg3_async" "fastapi-rs_pg2_sync" "FastAPI_asyncpg" "Go-Gin"; do
    port=${APPS[$label]}
    bench_one "$label" "$port" "/health"
    bench_one "$label" "$port" "/products/1"
    bench_one "$label" "$port" "/products?limit=10&offset=0"
    bench_one "$label" "$port" "/orders/1"
    bench_one "$label" "$port" "/cached/products/1"
    bench_one "$label" "$port" "/products" POST "$CREATE_BODY"
done
