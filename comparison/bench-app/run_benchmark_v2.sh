#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# E-commerce API Benchmark v2
# Compares: fastapi-rs, FastAPI+uvicorn, Go Gin, Go Echo, Node Fastify
# Tests: GET (x3 pagination variants) + GET single + POST + PATCH + DELETE
#        + auth + WebSocket.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BENCH_CLIENT="$PROJECT_ROOT/target/release/fastapi-rs-bench"

PYTHON_RS="python3"
PYTHON_FASTAPI="$PROJECT_ROOT/comparison/fastapi-venv/bin/python"

PORT_FASTAPI_RS=19001
PORT_FASTAPI=19002
PORT_GO_GIN=19003
PORT_FASTIFY=19004
PORT_GO_ECHO=19005

N=15000
WARMUP=500
WS_N=5000

PIDS=()
cleanup() {
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT

wait_for_port() {
    local port=$1 name=$2 timeout=15
    for i in $(seq 1 $timeout); do
        if curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    echo "TIMEOUT waiting for $name on :$port" >&2
    return 1
}

# ── Build / ensure all binaries ──────────────────────────────────────────
cd "$SCRIPT_DIR/go-gin" && go build -o ecommerce-gin . 2>&1
cd "$SCRIPT_DIR/go-echo-ecommerce" && go build -o ecommerce-echo . 2>&1
cd "$SCRIPT_DIR/fastify" && npm install --silent 2>&1
cd "$PROJECT_ROOT"
[ -x "$BENCH_CLIENT" ] || cargo build --release --bin fastapi-rs-bench 2>&1

# ── Start servers ────────────────────────────────────────────────────────
FASTAPI_RS_NO_SHIM=1 PORT=$PORT_FASTAPI_RS $PYTHON_RS "$SCRIPT_DIR/fastapi_rs_app.py" >/tmp/bench_rs.log 2>&1 &
PIDS+=($!)
PORT=$PORT_FASTAPI $PYTHON_FASTAPI "$SCRIPT_DIR/fastapi_app.py" >/tmp/bench_fa.log 2>&1 &
PIDS+=($!)
PORT=$PORT_GO_GIN "$SCRIPT_DIR/go-gin/ecommerce-gin" >/tmp/bench_gin.log 2>&1 &
PIDS+=($!)
PORT=$PORT_FASTIFY node "$SCRIPT_DIR/fastify/server.js" >/tmp/bench_fastify.log 2>&1 &
PIDS+=($!)
PORT=$PORT_GO_ECHO "$SCRIPT_DIR/go-echo-ecommerce/ecommerce-echo" >/tmp/bench_echo.log 2>&1 &
PIDS+=($!)

wait_for_port $PORT_FASTAPI_RS "fastapi-rs"
wait_for_port $PORT_FASTAPI    "FastAPI"
wait_for_port $PORT_GO_GIN     "Go-Gin"
wait_for_port $PORT_FASTIFY    "Fastify"
wait_for_port $PORT_GO_ECHO    "Go-Echo"

# ── Bench helpers ────────────────────────────────────────────────────────
run_bench() {
    local port=$1 path=$2 method=${3:-GET} body=${4:-} ctype=${5:-application/json}
    if [ "$method" = "GET" ]; then
        "$BENCH_CLIENT" 127.0.0.1 "$port" "$path" "$N" "$WARMUP" 2>&1
    else
        "$BENCH_CLIENT" 127.0.0.1 "$port" "$path" "$N" "$WARMUP" "$method" "$body" "$ctype" 2>&1
    fi
}

run_ws_bench() {
    local port=$1
    python3 -c "
import asyncio, json, time
async def bench():
    import websockets
    uri = 'ws://127.0.0.1:$port/ws/chat'
    n = $WS_N
    warmup = 200
    msg = json.dumps({'type':'chat','text':'hello world','seq':0})
    async with websockets.connect(uri) as ws:
        for _ in range(warmup):
            await ws.send(msg); await ws.recv()
        lats = []
        t0 = time.perf_counter()
        for i in range(n):
            payload = json.dumps({'type':'chat','text':'hello world','seq':i})
            s = time.perf_counter()
            await ws.send(payload); await ws.recv()
            lats.append((time.perf_counter()-s)*1e6)
        total = time.perf_counter()-t0
    lats.sort()
    print(f'  p50={lats[len(lats)//2]:.0f}μs p99={lats[int(len(lats)*0.99)]:.0f}μs min={lats[0]:.0f}μs | {n/total:.0f} msg/s')
asyncio.run(bench())
" 2>&1
}

ITEM_BODY='{"name":"Benchmark Item","price":42.99,"description":"test"}'
UPDATE_BODY='{"name":"Updated Item","price":99.99,"description":"updated"}'

FWS=("fastapi-rs:$PORT_FASTAPI_RS" "FastAPI:$PORT_FASTAPI" "Go-Gin:$PORT_GO_GIN" "Go-Echo:$PORT_GO_ECHO" "Fastify:$PORT_FASTIFY")

# ── Results TSV to stdout ────────────────────────────────────────────────
echo -e "framework\ttest\trps\tp50\tp99\tmin"

bench_row() {
    local fw=$1 test=$2 out=$3
    local rps=$(echo "$out" | grep -oE '[0-9]+ (req|msg)/s' | head -1 | cut -d' ' -f1)
    local p50=$(echo "$out" | grep -oE 'p50=[0-9]+' | head -1 | cut -d= -f2)
    local p99=$(echo "$out" | grep -oE 'p99=[0-9]+' | head -1 | cut -d= -f2)
    local mn=$(echo "$out" | grep -oE 'min=[0-9]+' | head -1 | cut -d= -f2)
    printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$fw" "$test" "${rps:-?}" "${p50:-?}" "${p99:-?}" "${mn:-?}"
}

for fw_port in "${FWS[@]}"; do
    fw="${fw_port%%:*}"
    port="${fw_port##*:}"

    echo "→ $fw" >&2

    bench_row "$fw" "GET /health"              "$(run_bench $port '/health')"
    bench_row "$fw" "GET /items?limit=1"       "$(run_bench $port '/items?limit=1&offset=0')"
    bench_row "$fw" "GET /items?limit=10"      "$(run_bench $port '/items?limit=10&offset=0')"
    bench_row "$fw" "GET /items?limit=100"     "$(run_bench $port '/items?limit=100&offset=0')"
    bench_row "$fw" "GET /items/1"             "$(run_bench $port '/items/1')"
    bench_row "$fw" "POST /items"              "$(run_bench $port '/items' POST \"$ITEM_BODY\")"
    bench_row "$fw" "PATCH /items/1"           "$(run_bench $port '/items/1' PATCH \"$UPDATE_BODY\")"
    bench_row "$fw" "DELETE /items/1"          "$(run_bench $port '/items/1' DELETE \"\")"
    bench_row "$fw" "WS /ws/chat"              "$(run_ws_bench $port)"
done
