# Shared helper for benchmark runners: resolve a Python interpreter
# that has ``fastapi_turbo`` installed before any subprocess is
# spawned. Honours the ``PY_RS`` env override; otherwise prefers the
# active venv; falls back to ``python3`` on PATH. Validates the
# resolved interpreter can actually import ``fastapi_turbo`` BEFORE
# the runner proceeds — earlier the bare ``PY_RS="python3"`` default
# silently picked up the wrong env and the runner appeared to "work"
# while measuring an unrelated stack (R34 audit caught this).
#
# Usage (from sibling runner shell scripts):
#
#   source "$SCRIPT_DIR/_resolve_py_rs.sh"
#
# After sourcing, ``$PY_RS`` is guaranteed to point at a Python
# whose ``import fastapi_turbo`` succeeds.

if [ -z "${PY_RS:-}" ]; then
    if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
        PY_RS="$VIRTUAL_ENV/bin/python"
    else
        PY_RS="$(command -v python3 || true)"
    fi
fi
if [ -z "${PY_RS:-}" ] || ! "$PY_RS" -c 'import fastapi_turbo' >/dev/null 2>&1; then
    echo "PY_RS=${PY_RS:-<unset>} cannot import fastapi_turbo." >&2
    echo "Set PY_RS to a python with fastapi_turbo installed, or" >&2
    echo "activate the venv first." >&2
    exit 2
fi
export PY_RS
