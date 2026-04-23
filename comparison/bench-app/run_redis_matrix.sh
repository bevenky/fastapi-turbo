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
trap 'for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; wait "$p" 2>/dev/null || true; done' EXIT

wait_port() {
    local port=$1
    for _ in $(seq 1 20); do
        curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
        sleep 0.5
    done
    return 1
}

FASTAPI_RS_NO_SHIM=1 PORT=19040 $PY_RS "$SCRIPT_DIR/redis_sync_app.py"       >/tmp/redis_19040.log 2>&1 &
PIDS+=($!)
FASTAPI_RS_NO_SHIM=1 PORT=19041 $PY_RS "$SCRIPT_DIR/redis_async_app.py"      >/tmp/redis_19041.log 2>&1 &
PIDS+=($!)
PORT=19042 $PY_FA "$SCRIPT_DIR/redis_fastapi_uvicorn_app.py"                  >/tmp/redis_19042.log 2>&1 &
PIDS+=($!)

for p in 19040 19041 19042; do
    wait_port $p || { echo "Failed :$p" >&2; tail -5 /tmp/redis_${p}.log >&2; exit 1; }
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
bench_one "fastapi-rs_sync(redis-py)" 19040 "/health"
bench_one "fastapi-rs_sync(redis-py)" 19040 "/cache/get"
bench_one "fastapi-rs_sync(redis-py)" 19040 "/cache/set" POST "{}"
bench_one "fastapi-rs_async(redis.asyncio)" 19041 "/health"
bench_one "fastapi-rs_async(redis.asyncio)" 19041 "/cache/get"
bench_one "fastapi-rs_async(redis.asyncio)" 19041 "/cache/set" POST "{}"
bench_one "FastAPI_uvicorn(redis.asyncio)" 19042 "/health"
bench_one "FastAPI_uvicorn(redis.asyncio)" 19042 "/cache/get"
bench_one "FastAPI_uvicorn(redis.asyncio)" 19042 "/cache/set" POST "{}"
