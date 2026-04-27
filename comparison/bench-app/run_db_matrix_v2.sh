#!/usr/bin/env bash
# DB driver matrix v2 — adds Rust Axum baseline.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BENCH="$PROJECT_ROOT/target/release/fastapi-turbo-bench"
# Resolve PY_RS via the shared helper (R34) — verifies the Python
# can actually import fastapi_turbo BEFORE running anything.
source "$SCRIPT_DIR/_resolve_py_rs.sh"
source "$SCRIPT_DIR/_bench_row.sh"
PY_FA="$PROJECT_ROOT/comparison/fastapi-venv/bin/python"

N=5000
WARMUP=200

PIDS=()
cleanup() {
    for p in "${PIDS[@]}"; do kill -TERM "$p" 2>/dev/null || true; done
    for _ in 1 2 3; do
        local any=0
        for p in "${PIDS[@]}"; do kill -0 "$p" 2>/dev/null && any=1; done
        [ "$any" = "0" ] && return 0
        sleep 1
    done
    for p in "${PIDS[@]}"; do kill -KILL "$p" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM HUP

wait_port() {
    local port=$1 name=$2
    for _ in $(seq 1 30); do
        curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
        sleep 0.5
    done
    echo "TIMEOUT $name:$port" >&2
    return 1
}

# Build Rust Axum DB if missing
[ -x "$SCRIPT_DIR/db_rust_axum/target/release/db-axum" ] || (cd "$SCRIPT_DIR/db_rust_axum" && PATH="$HOME/.cargo/bin:$PATH" cargo build --release 2>&1 | tail -1)
[ -x "$SCRIPT_DIR/db-gin" ] || (cd "$SCRIPT_DIR" && go build -o db-gin db_go_gin.go)

FASTAPI_TURBO_NO_SHIM=1 PORT=19030 $PY_RS "$SCRIPT_DIR/db_fastapi_turbo_app.py"      >/tmp/dbm_19030.log 2>&1 & PIDS+=($!)
FASTAPI_TURBO_NO_SHIM=1 PORT=19032 $PY_RS "$SCRIPT_DIR/db_async_psycopg3_app.py"  >/tmp/dbm_19032.log 2>&1 & PIDS+=($!)
FASTAPI_TURBO_NO_SHIM=1 PORT=19033 $PY_RS "$SCRIPT_DIR/db_sync_fastapi_turbo_app.py" >/tmp/dbm_19033.log 2>&1 & PIDS+=($!)
PORT=19034 $PY_FA "$SCRIPT_DIR/db_fastapi_uvicorn_app.py"                     >/tmp/dbm_19034.log 2>&1 & PIDS+=($!)
PORT=19031 "$SCRIPT_DIR/db-gin"                                              >/tmp/dbm_19031.log 2>&1 & PIDS+=($!)
PORT=19036 "$SCRIPT_DIR/db_rust_axum/target/release/db-axum"                 >/tmp/dbm_19036.log 2>&1 & PIDS+=($!)

for p in 19030 19031 19032 19033 19034 19036; do
    wait_port $p "p$p" || exit 1
done

bench_one() {
    local label=$1 port=$2 path=$3 method=${4:-GET} body=${5:-}
    local out
    if [ "$method" = "GET" ]; then
        out="$($BENCH 127.0.0.1 "$port" "$path" "$N" "$WARMUP" 2>&1)"
    else
        out="$($BENCH 127.0.0.1 "$port" "$path" "$N" "$WARMUP" "$method" "$body" "application/json" 2>&1)"
    fi
    bench_row "$label" "$path ($method)" "$out"
}

CREATE_BODY='{"name":"Bench","price":42.99,"category_id":1,"stock":10}'
echo -e "label\tendpoint\trps\tp50\tp99"

for pair in "Go-Gin:19031" \
            "Rust-Axum:19036" \
            "fastapi-turbo_pg3_sync:19030" \
            "fastapi-turbo_pg2_sync:19033" \
            "fastapi-turbo_pg3_async:19032" \
            "FastAPI_asyncpg:19034"; do
    label="${pair%:*}"; port="${pair##*:}"
    bench_one "$label" "$port" "/health"
    bench_one "$label" "$port" "/products/1"
    bench_one "$label" "$port" "/products?limit=10&offset=0"
    bench_one "$label" "$port" "/orders/1"
    bench_one "$label" "$port" "/cached/products/1"
    bench_one "$label" "$port" "/products" POST "$CREATE_BODY"
done
