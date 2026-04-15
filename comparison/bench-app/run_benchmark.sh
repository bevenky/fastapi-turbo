#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Mini E-commerce API Benchmark
# Compares: fastapi-rs, FastAPI+uvicorn, Go Gin, Node Fastify
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BENCH_CLIENT="$PROJECT_ROOT/target/release/fastapi-rs-bench"

# Python interpreters
# fastapi-rs is installed in the default env; real FastAPI lives in a venv
PYTHON_RS="python3"
PYTHON_FASTAPI="$PROJECT_ROOT/comparison/fastapi-venv/bin/python"

# Verify the FastAPI venv exists, fall back to pip-installing if needed
if [ ! -x "$PYTHON_FASTAPI" ]; then
    echo "FastAPI venv not found at $PYTHON_FASTAPI"
    echo "Creating venv and installing fastapi + uvicorn..."
    python3 -m venv "$PROJECT_ROOT/comparison/fastapi-venv"
    PYTHON_FASTAPI="$PROJECT_ROOT/comparison/fastapi-venv/bin/python"
    "$PYTHON_FASTAPI" -m pip install --quiet fastapi uvicorn
fi

# Ports
PORT_FASTAPI_RS=19001
PORT_FASTAPI=19002
PORT_GO_GIN=19003
PORT_FASTIFY=19004

# Bench parameters
N=15000
WARMUP=500
WS_N=5000

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Track PIDs for cleanup
PIDS=()

cleanup() {
    echo ""
    echo -e "${YELLOW}Cleaning up...${NC}"
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    echo -e "${GREEN}All servers stopped.${NC}"
}
trap cleanup EXIT

wait_for_port() {
    local port=$1 name=$2 timeout=15
    echo -n "  Waiting for $name on :$port "
    for i in $(seq 1 $timeout); do
        if curl -s "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
            echo -e " ${GREEN}ready${NC}"
            return 0
        fi
        echo -n "."
        sleep 1
    done
    echo -e " ${RED}TIMEOUT${NC}"
    return 1
}

# ===================================================================
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  Mini E-commerce API Benchmark${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

# -------------------------------------------------------------------
# Step 1: Build Go binary
# -------------------------------------------------------------------
echo -e "${YELLOW}[1/4] Building Go Gin server...${NC}"
cd "$SCRIPT_DIR/go-gin"
go build -o ecommerce-gin . 2>&1
echo -e "  ${GREEN}Built go-gin/ecommerce-gin${NC}"

# -------------------------------------------------------------------
# Step 2: Install Node dependencies
# -------------------------------------------------------------------
echo -e "${YELLOW}[2/4] Installing Fastify dependencies...${NC}"
cd "$SCRIPT_DIR/fastify"
npm install --silent 2>&1
echo -e "  ${GREEN}node_modules ready${NC}"

# -------------------------------------------------------------------
# Step 3: Verify Rust bench client
# -------------------------------------------------------------------
echo -e "${YELLOW}[3/4] Checking bench client...${NC}"
if [ ! -x "$BENCH_CLIENT" ]; then
    echo -e "  ${RED}Bench client not found at $BENCH_CLIENT${NC}"
    echo "  Building with: cargo build --release --bin fastapi-rs-bench"
    cd "$PROJECT_ROOT"
    cargo build --release --bin fastapi-rs-bench 2>&1
fi
echo -e "  ${GREEN}Bench client ready${NC}"

# -------------------------------------------------------------------
# Step 4: Start all servers
# -------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[4/4] Starting servers...${NC}"

# fastapi-rs (uses default Python where fastapi-rs is installed)
cd "$PROJECT_ROOT"
FASTAPI_RS_NO_SHIM=1 PORT=$PORT_FASTAPI_RS $PYTHON_RS "$SCRIPT_DIR/fastapi_rs_app.py" &
PIDS+=($!)

# FastAPI + uvicorn (uses the fastapi venv)
PORT=$PORT_FASTAPI $PYTHON_FASTAPI "$SCRIPT_DIR/fastapi_app.py" &
PIDS+=($!)

# Go Gin
PORT=$PORT_GO_GIN "$SCRIPT_DIR/go-gin/ecommerce-gin" &
PIDS+=($!)

# Fastify
PORT=$PORT_FASTIFY node "$SCRIPT_DIR/fastify/server.js" &
PIDS+=($!)

# Wait for all servers
wait_for_port $PORT_FASTAPI_RS "fastapi-rs"
wait_for_port $PORT_FASTAPI    "FastAPI"
wait_for_port $PORT_GO_GIN     "Go Gin"
wait_for_port $PORT_FASTIFY    "Fastify"

echo ""

# ===================================================================
# Helper: run bench and capture output
# ===================================================================

# The Rust bench client: HOST PORT PATH N WARMUP METHOD BODY CONTENT_TYPE
# For GET requests with auth headers, we use a Python helper since the
# Rust client only supports basic HTTP headers.

run_bench() {
    local label=$1 host=127.0.0.1 port=$2 path=$3 method=${4:-GET} body=${5:-} ctype=${6:-application/json}
    echo -n "  $label: "
    if [ "$method" = "GET" ]; then
        "$BENCH_CLIENT" "$host" "$port" "$path" "$N" "$WARMUP" 2>&1
    else
        "$BENCH_CLIENT" "$host" "$port" "$path" "$N" "$WARMUP" "$method" "$body" "$ctype" 2>&1
    fi
}

# For auth-required endpoints, the Rust bench client doesn't support custom
# headers. We use a Python one-liner that speaks raw TCP with keep-alive.
run_bench_auth() {
    local label=$1 port=$2 path=$3
    echo -n "  $label: "
    python3 -c "
import socket, time

host = '127.0.0.1'
port = $port
path = '$path'
n = $N
warmup = $WARMUP

req = (
    f'GET {path} HTTP/1.1\r\n'
    f'Host: {host}\r\n'
    f'Connection: keep-alive\r\n'
    f'Authorization: Bearer secret-token-123\r\n'
    f'\r\n'
).encode()

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
sock.connect((host, port))

def send_recv(s, data):
    s.sendall(data)
    resp = b''
    while True:
        chunk = s.recv(16384)
        if not chunk:
            break
        resp += chunk
        idx = resp.find(b'\r\n\r\n')
        if idx >= 0:
            headers = resp[:idx].decode('utf-8', errors='replace')
            cl = 0
            for line in headers.split('\r\n'):
                if line.lower().startswith('content-length:'):
                    cl = int(line.split(':',1)[1].strip())
            body_start = idx + 4
            if len(resp) >= body_start + cl:
                break
    return resp

# Warmup
for _ in range(warmup):
    send_recv(sock, req)

lats = []
t0 = time.perf_counter()
for _ in range(n):
    s = time.perf_counter()
    send_recv(sock, req)
    lats.append((time.perf_counter() - s) * 1e6)
total = time.perf_counter() - t0

lats.sort()
p50 = lats[len(lats)//2]
p99 = lats[int(len(lats)*0.99)]
mn = lats[0]
rps = n / total
print(f'  client p50={p50:.0f}\u03bcs p99={p99:.0f}\u03bcs min={mn:.0f}\u03bcs | {rps:.0f} req/s')
sock.close()
" 2>&1
}

# For form-encoded POST
run_bench_form() {
    local label=$1 port=$2 path=$3 body=$4
    echo -n "  $label: "
    "$BENCH_CLIENT" 127.0.0.1 "$port" "$path" "$N" "$WARMUP" POST "$body" "application/x-www-form-urlencoded" 2>&1
}

# ===================================================================
# WebSocket benchmark using Python websockets library
# ===================================================================
run_ws_bench() {
    local label=$1 port=$2
    echo -n "  $label: "
    python3 -c "
import asyncio, json, time

async def bench():
    import websockets
    uri = 'ws://127.0.0.1:$port/ws/chat'
    n = $WS_N
    warmup = 200
    msg = json.dumps({'type': 'chat', 'text': 'hello world', 'seq': 0})

    async with websockets.connect(uri) as ws:
        # Warmup
        for i in range(warmup):
            await ws.send(msg)
            await ws.recv()

        lats = []
        t0 = time.perf_counter()
        for i in range(n):
            payload = json.dumps({'type': 'chat', 'text': 'hello world', 'seq': i})
            s = time.perf_counter()
            await ws.send(payload)
            resp = await ws.recv()
            lats.append((time.perf_counter() - s) * 1e6)
            data = json.loads(resp)
            assert 'server_ts' in data, f'No server_ts in response: {resp}'
        total = time.perf_counter() - t0

    lats.sort()
    p50 = lats[len(lats)//2]
    p99 = lats[int(len(lats)*0.99)]
    mn = lats[0]
    rps = n / total
    print(f'  p50={p50:.0f}\u03bcs p99={p99:.0f}\u03bcs min={mn:.0f}\u03bcs | {rps:.0f} msg/s')

asyncio.run(bench())
" 2>&1
}

# ===================================================================
# Run all benchmarks
# ===================================================================

ITEM_BODY='{"name":"Benchmark Item","price":42.99,"description":"test"}'
UPDATE_BODY='{"name":"Updated Item","price":99.99,"description":"updated"}'
FORM_BODY='username=admin&password=secret'

declare -A FRAMEWORKS
FRAMEWORKS=( ["fastapi-rs"]=$PORT_FASTAPI_RS ["FastAPI"]=$PORT_FASTAPI ["Go-Gin"]=$PORT_GO_GIN ["Fastify"]=$PORT_FASTIFY )
ORDERED_NAMES=("fastapi-rs" "FastAPI" "Go-Gin" "Fastify")

# Result arrays (associative)
declare -A RESULTS

for fw in "${ORDERED_NAMES[@]}"; do
    port=${FRAMEWORKS[$fw]}
    echo ""
    echo -e "${CYAN}=== $fw  (port $port) ===${NC}"

    echo -e "${YELLOW}GET /health ($N requests):${NC}"
    RESULTS["$fw,health"]=$(run_bench "$fw" "$port" "/health" 2>&1)
    echo "${RESULTS["$fw,health"]}"

    echo -e "${YELLOW}GET /items?limit=10 ($N requests):${NC}"
    RESULTS["$fw,list"]=$(run_bench "$fw" "$port" "/items?limit=10&offset=0" 2>&1)
    echo "${RESULTS["$fw,list"]}"

    echo -e "${YELLOW}GET /items/1 ($N requests):${NC}"
    RESULTS["$fw,get"]=$(run_bench "$fw" "$port" "/items/1" 2>&1)
    echo "${RESULTS["$fw,get"]}"

    echo -e "${YELLOW}POST /items ($N requests):${NC}"
    RESULTS["$fw,create"]=$(run_bench "$fw" "$port" "/items" POST "$ITEM_BODY" 2>&1)
    echo "${RESULTS["$fw,create"]}"

    echo -e "${YELLOW}PUT /items/1 ($N requests):${NC}"
    RESULTS["$fw,update"]=$(run_bench "$fw" "$port" "/items/1" PUT "$UPDATE_BODY" 2>&1)
    echo "${RESULTS["$fw,update"]}"

    echo -e "${YELLOW}DELETE /items/1 ($N requests):${NC}"
    RESULTS["$fw,delete"]=$(run_bench "$fw" "$port" "/items/1" DELETE "" 2>&1)
    echo "${RESULTS["$fw,delete"]}"

    echo -e "${YELLOW}GET /users/me with auth ($N requests):${NC}"
    RESULTS["$fw,auth"]=$(run_bench_auth "$fw" "$port" "/users/me" 2>&1)
    echo "${RESULTS["$fw,auth"]}"

    echo -e "${YELLOW}WS /ws/chat echo ($WS_N messages):${NC}"
    RESULTS["$fw,ws"]=$(run_ws_bench "$fw" "$port" 2>&1)
    echo "${RESULTS["$fw,ws"]}"
done

# ===================================================================
# Print markdown comparison table
# ===================================================================

echo ""
echo ""
echo -e "${CYAN}====================================================================${NC}"
echo -e "${CYAN}  COMPARISON TABLE${NC}"
echo -e "${CYAN}====================================================================${NC}"
echo ""

# Extract req/s from result strings
extract_rps() {
    echo "$1" | grep -oE '[0-9]+ (req|msg)/s' | head -1 | grep -oE '^[0-9]+'
}

extract_p50() {
    echo "$1" | grep -oE 'p50=[0-9]+' | head -1 | grep -oE '[0-9]+'
}

extract_p99() {
    echo "$1" | grep -oE 'p99=[0-9]+' | head -1 | grep -oE '[0-9]+'
}

echo "### Throughput (req/s) -- higher is better"
echo ""
printf "| %-22s |" "Endpoint"
for fw in "${ORDERED_NAMES[@]}"; do
    printf " %-14s |" "$fw"
done
echo ""

printf "| %-22s |" "----------------------"
for fw in "${ORDERED_NAMES[@]}"; do
    printf " %-14s |" "--------------"
done
echo ""

for test in health list get create update delete auth ws; do
    case $test in
        health) label="GET /health";;
        list)   label="GET /items?limit=10";;
        get)    label="GET /items/1";;
        create) label="POST /items";;
        update) label="PUT /items/1";;
        delete) label="DELETE /items/1";;
        auth)   label="GET /users/me (auth)";;
        ws)     label="WS /ws/chat";;
    esac

    printf "| %-22s |" "$label"
    for fw in "${ORDERED_NAMES[@]}"; do
        rps=$(extract_rps "${RESULTS["$fw,$test"]}" 2>/dev/null || echo "?")
        printf " %-14s |" "${rps:-?}"
    done
    echo ""
done

echo ""
echo "### Latency p50 (us) -- lower is better"
echo ""
printf "| %-22s |" "Endpoint"
for fw in "${ORDERED_NAMES[@]}"; do
    printf " %-14s |" "$fw"
done
echo ""

printf "| %-22s |" "----------------------"
for fw in "${ORDERED_NAMES[@]}"; do
    printf " %-14s |" "--------------"
done
echo ""

for test in health list get create update delete auth ws; do
    case $test in
        health) label="GET /health";;
        list)   label="GET /items?limit=10";;
        get)    label="GET /items/1";;
        create) label="POST /items";;
        update) label="PUT /items/1";;
        delete) label="DELETE /items/1";;
        auth)   label="GET /users/me (auth)";;
        ws)     label="WS /ws/chat";;
    esac

    printf "| %-22s |" "$label"
    for fw in "${ORDERED_NAMES[@]}"; do
        p50=$(extract_p50 "${RESULTS["$fw,$test"]}" 2>/dev/null || echo "?")
        printf " %-14s |" "${p50:-?}"
    done
    echo ""
done

echo ""
echo "### Latency p99 (us) -- lower is better"
echo ""
printf "| %-22s |" "Endpoint"
for fw in "${ORDERED_NAMES[@]}"; do
    printf " %-14s |" "$fw"
done
echo ""

printf "| %-22s |" "----------------------"
for fw in "${ORDERED_NAMES[@]}"; do
    printf " %-14s |" "--------------"
done
echo ""

for test in health list get create update delete auth ws; do
    case $test in
        health) label="GET /health";;
        list)   label="GET /items?limit=10";;
        get)    label="GET /items/1";;
        create) label="POST /items";;
        update) label="PUT /items/1";;
        delete) label="DELETE /items/1";;
        auth)   label="GET /users/me (auth)";;
        ws)     label="WS /ws/chat";;
    esac

    printf "| %-22s |" "$label"
    for fw in "${ORDERED_NAMES[@]}"; do
        p99=$(extract_p99 "${RESULTS["$fw,$test"]}" 2>/dev/null || echo "?")
        printf " %-14s |" "${p99:-?}"
    done
    echo ""
done

echo ""
echo -e "${GREEN}Benchmark complete!${NC}"
