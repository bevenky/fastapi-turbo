#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Database Benchmark Runner (wrapper for bench_db.py)
# Compares: fastapi-rs vs Go Gin with PostgreSQL + Redis
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Starting database benchmark..."
echo "  PostgreSQL: localhost:5432/fastapi_rs_bench"
echo "  Redis: localhost:6379"
echo ""

exec python3 "$SCRIPT_DIR/bench_db.py" "$@"
