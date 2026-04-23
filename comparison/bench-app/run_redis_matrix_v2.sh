#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BENCH="$PROJECT_ROOT/target/release/fastapi-rs-bench"
PY_RS="python3"
PY_FA="$PROJECT_ROOT/comparison/fastapi-venv/bin/python"

N=10000
WARMUP=500

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
    local port=$1
    for _ in $(seq 1 30); do
        curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
        sleep 0.5
    done
    return 1
}

# Build Rust Axum Redis if missing
[ -x "$SCRIPT_DIR/rust-axum-redis/target/release/redis-axum" ] || (cd "$SCRIPT_DIR/rust-axum-redis" && PATH="$HOME/.cargo/bin:$PATH" cargo build --release 2>&1 | tail -1)

FASTAPI_RS_NO_SHIM=1 PORT=19040 $PY_RS "$SCRIPT_DIR/redis_sync_app.py"       >/tmp/r_19040.log 2>&1 & PIDS+=($!)
FASTAPI_RS_NO_SHIM=1 PORT=19041 $PY_RS "$SCRIPT_DIR/redis_async_app.py"      >/tmp/r_19041.log 2>&1 & PIDS+=($!)
PORT=19042 $PY_FA "$SCRIPT_DIR/redis_fastapi_uvicorn_app.py"                  >/tmp/r_19042.log 2>&1 & PIDS+=($!)
PORT=19043 "$SCRIPT_DIR/rust-axum-redis/target/release/redis-axum"           >/tmp/r_19043.log 2>&1 & PIDS+=($!)

for p in 19040 19041 19042 19043; do
    wait_port $p || { echo "Failed :$p" >&2; tail -5 /tmp/r_${p}.log >&2; exit 1; }
done

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

echo -e "label\tendpoint\trps\tp50\tp99"
for pair in "Rust-Axum:19043" \
            "fastapi-rs_sync(redis-py):19040" \
            "fastapi-rs_async(redis.asyncio):19041" \
            "FastAPI_uvicorn(redis.asyncio):19042"; do
    label="${pair%:*}"; port="${pair##*:}"
    bench_one "$label" "$port" "/health"
    bench_one "$label" "$port" "/cache/get"
    bench_one "$label" "$port" "/cache/set" POST "{}"
done
