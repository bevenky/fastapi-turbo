#!/usr/bin/env bash
# v3: adds Rust Axum baseline + reordered columns.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BENCH="$PROJECT_ROOT/target/release/fastapi-rs-bench"
PY_RS="python3"
PY_FA="$PROJECT_ROOT/comparison/fastapi-venv/bin/python"

P_RS=19001
P_FA=19002
P_GIN=19003
P_FY=19004
P_ECHO=19005
P_AXUM=19006

N=15000
WARMUP=500
WS_N=5000

PIDS=()
trap 'for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; wait "$p" 2>/dev/null || true; done' EXIT

wait_port() {
    local port=$1 name=$2
    for _ in $(seq 1 15); do
        curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
        sleep 1
    done
    echo "TIMEOUT $name:$port" >&2
    return 1
}

# ── build ──
cd "$SCRIPT_DIR/go-gin" && go build -o ecommerce-gin . 2>&1
cd "$SCRIPT_DIR/go-echo-ecommerce" && go build -o ecommerce-echo . 2>&1
cd "$SCRIPT_DIR/fastify" && npm install --silent 2>&1
cd "$SCRIPT_DIR/rust-axum-ecommerce" && PATH="$HOME/.cargo/bin:$PATH" cargo build --release 2>&1 | tail -1
cd "$PROJECT_ROOT"

# ── start ──
FASTAPI_RS_NO_SHIM=1 PORT=$P_RS $PY_RS "$SCRIPT_DIR/fastapi_rs_app.py" >/tmp/rs.log 2>&1 & PIDS+=($!)
PORT=$P_FA $PY_FA "$SCRIPT_DIR/fastapi_app.py" >/tmp/fa.log 2>&1 & PIDS+=($!)
PORT=$P_GIN "$SCRIPT_DIR/go-gin/ecommerce-gin" >/tmp/gin.log 2>&1 & PIDS+=($!)
PORT=$P_FY node "$SCRIPT_DIR/fastify/server.js" >/tmp/fy.log 2>&1 & PIDS+=($!)
PORT=$P_ECHO "$SCRIPT_DIR/go-echo-ecommerce/ecommerce-echo" >/tmp/echo.log 2>&1 & PIDS+=($!)
PORT=$P_AXUM "$SCRIPT_DIR/rust-axum-ecommerce/target/release/ecommerce-axum" >/tmp/axum.log 2>&1 & PIDS+=($!)

wait_port $P_RS   fastapi-rs
wait_port $P_FA   FastAPI
wait_port $P_GIN  Go-Gin
wait_port $P_FY   Fastify
wait_port $P_ECHO Go-Echo
wait_port $P_AXUM Rust-Axum

bench() {
    local port=$1 path=$2 method=${3:-GET} body=${4:-}
    if [ "$method" = "GET" ]; then
        "$BENCH" 127.0.0.1 "$port" "$path" "$N" "$WARMUP" 2>&1
    else
        "$BENCH" 127.0.0.1 "$port" "$path" "$N" "$WARMUP" "$method" "$body" "application/json" 2>&1
    fi
}

ws_bench() {
    local port=$1
    python3 -c "
import asyncio, json, time, websockets
async def b():
    async with websockets.connect('ws://127.0.0.1:$port/ws/chat') as ws:
        for _ in range(200):
            await ws.send(json.dumps({'type':'chat','text':'hi','seq':0})); await ws.recv()
        lats=[]; t0=time.perf_counter()
        for i in range($WS_N):
            s=time.perf_counter(); await ws.send(json.dumps({'type':'chat','text':'hi','seq':i})); await ws.recv()
            lats.append((time.perf_counter()-s)*1e6)
        total=time.perf_counter()-t0
    lats.sort()
    print(f'  p50={lats[len(lats)//2]:.0f}μs p99={lats[int(len(lats)*0.99)]:.0f}μs min={lats[0]:.0f}μs | {$WS_N/total:.0f} msg/s')
asyncio.run(b())
"
}

row() {
    local fw=$1 test=$2 out=$3
    local rps=$(echo "$out" | grep -oE '[0-9]+ (req|msg)/s' | head -1 | cut -d' ' -f1)
    local p50=$(echo "$out" | grep -oE 'p50=[0-9]+' | head -1 | cut -d= -f2)
    local p99=$(echo "$out" | grep -oE 'p99=[0-9]+' | head -1 | cut -d= -f2)
    printf "%s\t%s\t%s\t%s\t%s\n" "$fw" "$test" "${rps:-?}" "${p50:-?}" "${p99:-?}"
}

BODY='{"name":"X","price":42.99,"description":"t"}'
UBODY='{"name":"U","price":99.99,"description":"u"}'

echo -e "framework\ttest\trps\tp50\tp99"

for fw_port in "Go-Gin:$P_GIN" "Go-Echo:$P_ECHO" "Rust-Axum:$P_AXUM" "Fastify:$P_FY" "fastapi-rs:$P_RS" "FastAPI:$P_FA"; do
    fw="${fw_port%:*}"; port="${fw_port##*:}"
    echo "→ $fw" >&2
    row "$fw" "GET /health"              "$(bench $port '/health')"
    row "$fw" "GET /items?limit=1"        "$(bench $port '/items?limit=1&offset=0')"
    row "$fw" "GET /items?limit=10"       "$(bench $port '/items?limit=10&offset=0')"
    row "$fw" "GET /items?limit=100"      "$(bench $port '/items?limit=100&offset=0')"
    row "$fw" "GET /items/1"              "$(bench $port '/items/1')"
    row "$fw" "POST /items"               "$(bench $port '/items' POST \"$BODY\")"
    row "$fw" "PATCH /items/1"            "$(bench $port '/items/1' PATCH \"$UBODY\")"
    row "$fw" "DELETE /items/1"           "$(bench $port '/items/1' DELETE '')"
    row "$fw" "WS /ws/chat"               "$(ws_bench $port)"
done
