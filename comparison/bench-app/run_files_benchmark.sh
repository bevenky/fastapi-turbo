#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# File handling benchmark — upload + FileResponse + StaticFiles
# Compares: fastapi-turbo, Go Gin, Node Fastify
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PORT_RS=18501
PORT_GO=18502
PORT_JS=18503

YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

PIDS=()
cleanup() {
    echo ""
    echo -e "${YELLOW}Cleaning up...${NC}"
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT

wait_for_port() {
    local port=$1 name=$2
    echo -n "  Waiting for $name on :$port "
    for _ in $(seq 1 20); do
        if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
            echo -e " ${GREEN}ready${NC}"
            return 0
        fi
        echo -n "."
        sleep 0.5
    done
    echo -e " ${RED}TIMEOUT${NC}"
    return 1
}

echo -e "${CYAN}=== Building Go Gin file bench server ===${NC}"
cd "$SCRIPT_DIR"
go build -o files-gin files_go_gin.go

echo -e "${CYAN}=== Starting servers ===${NC}"

# fastapi-turbo
PORT=$PORT_RS python "$SCRIPT_DIR/files_fastapi_turbo.py" >/tmp/files_rs.log 2>&1 &
PIDS+=($!)

# Go Gin
PORT=$PORT_GO "$SCRIPT_DIR/files-gin" >/tmp/files_go.log 2>&1 &
PIDS+=($!)

# Fastify
cd "$SCRIPT_DIR/fastify"
PORT=$PORT_JS node files_fastify.js >/tmp/files_js.log 2>&1 &
PIDS+=($!)
cd "$SCRIPT_DIR"

wait_for_port $PORT_RS "fastapi-turbo" || { cat /tmp/files_rs.log; exit 1; }
wait_for_port $PORT_GO "Go-Gin"     || { cat /tmp/files_go.log; exit 1; }
wait_for_port $PORT_JS "Fastify"    || { cat /tmp/files_js.log; exit 1; }

echo ""
echo -e "${CYAN}=== Running benchmark ===${NC}"
python "$SCRIPT_DIR/bench_files.py" "$PORT_RS,$PORT_GO,$PORT_JS"
