#!/usr/bin/env bash
# Local mirror of the release-required external compatibility gates
# that ``.github/workflows/ci.yml`` and ``release.yml`` run on every
# PR / push / tag. Auditors can run this locally to verify the same
# gates without waiting for CI.
#
# Force-resets ``/tmp/fastapi_upstream`` and ``/tmp/sentry-python``
# to the pinned tags so the local run cannot drift to a different
# upstream version (R28 caught a /tmp/sentry-python drift; R31
# extracts the same logic into this script for hand-runnability;
# R32 makes the python interpreter explicit so the script can't
# silently pick up a venv that lacks ``fastapi_turbo``).
#
# Usage:
#   ./scripts/run_external_compat_gates.sh           # run both gates
#   ./scripts/run_external_compat_gates.sh fastapi   # upstream FastAPI only
#   ./scripts/run_external_compat_gates.sh sentry    # Sentry only
#
# Resolution of the Python interpreter (highest precedence first):
#   1. ``$PYTHON_BIN`` env var if set.
#   2. ``$VIRTUAL_ENV/bin/python`` if a venv is active.
#   3. ``python`` on PATH.
#
# In all cases the script then verifies ``fastapi_turbo`` imports
# from the resolved interpreter BEFORE running any pip / pytest —
# the audit caught a case where ``$VIRTUAL_ENV`` pointed at a
# different env than the one that had the package, defeating the
# whole "local-gate doesn't drift" guarantee.

set -euo pipefail

UPSTREAM_TAG="0.136.0"
SENTRY_TAG="2.42.0"

GATE="${1:-all}"

if [ -n "${PYTHON_BIN:-}" ]; then
    : # honour explicit override
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    PYTHON_BIN="$VIRTUAL_ENV/bin/python"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
else
    echo "no python interpreter found; set PYTHON_BIN" >&2
    exit 2
fi

if ! "$PYTHON_BIN" -c 'import fastapi_turbo' >/dev/null 2>&1; then
    echo "${PYTHON_BIN} cannot import fastapi_turbo." >&2
    echo "Run \`\$PYTHON_BIN -m pip install -e .\` or" >&2
    echo "\`maturin develop\` in the venv first." >&2
    exit 2
fi

echo "── External compat gates ── using ${PYTHON_BIN} ──"
"$PYTHON_BIN" -c 'import sys; print("sys.executable:", sys.executable); import fastapi_turbo; print("fastapi_turbo from:", fastapi_turbo.__file__)'

# Offline mode: when ``OFFLINE=1`` is set, skip the network-fetch
# steps and verify the existing ``/tmp/...`` checkout is already at
# the pinned tag. R33 added this for network-restricted audit
# environments where a hand-prepared checkout is the only option.
OFFLINE="${OFFLINE:-0}"

verify_at_tag() {
    local tree="$1" tag="$2"
    if [ ! -d "$tree/.git" ]; then
        echo "OFFLINE=1 set, but $tree is not a git checkout." >&2
        echo "Pre-clone the repo and check out tag $tag, then re-run." >&2
        exit 2
    fi
    local current_tag
    current_tag="$(git -C "$tree" describe --tags --exact-match HEAD 2>/dev/null || true)"
    local current_sha
    current_sha="$(git -C "$tree" rev-parse HEAD 2>/dev/null)"
    local pinned_sha
    pinned_sha="$(git -C "$tree" rev-parse "$tag" 2>/dev/null || true)"
    if [ "$current_tag" = "$tag" ] || [ "$current_sha" = "$pinned_sha" ]; then
        echo "OFFLINE: $tree already at $tag — proceeding."
        return 0
    fi
    echo "OFFLINE=1 set, but $tree is at \"$current_tag\" / $current_sha," >&2
    echo "expected tag $tag (sha $pinned_sha)." >&2
    echo "Run \`git -C $tree fetch --tags && git -C $tree reset --hard $tag\`" >&2
    echo "while online, or unset OFFLINE." >&2
    exit 2
}

run_fastapi_gate() {
    echo "── Upstream FastAPI ${UPSTREAM_TAG} suite under shim ──"
    if [ "$OFFLINE" = "1" ]; then
        verify_at_tag /tmp/fastapi_upstream "$UPSTREAM_TAG"
    else
        if [ ! -d /tmp/fastapi_upstream/.git ]; then
            rm -rf /tmp/fastapi_upstream
            git clone https://github.com/fastapi/fastapi /tmp/fastapi_upstream
        fi
        git -C /tmp/fastapi_upstream fetch --tags --force --depth 1 origin "$UPSTREAM_TAG"
        git -C /tmp/fastapi_upstream reset --hard "$UPSTREAM_TAG"
        git -C /tmp/fastapi_upstream clean -fdx -- ':!conftest.py'
        "$PYTHON_BIN" -m pip install -q pytest-asyncio pyyaml dirty-equals \
                                       "sqlmodel>=0.0.14" inline-snapshot
    fi

    cat > /tmp/fastapi_upstream/conftest.py <<'PY'
# Auto-injected by run_external_compat_gates.sh: install the
# ``from fastapi import ...`` shim into THIS pytest process so
# upstream tests resolve to fastapi-turbo.
import fastapi_turbo  # noqa: F401
PY

    # cwd into the upstream root so test_tutorial cwd-relative
    # asset lookups (open("docs_src/...")) resolve.
    (cd /tmp/fastapi_upstream && "$PYTHON_BIN" -m pytest tests/ -q --tb=no)
}

run_sentry_gate() {
    echo "── Sentry SDK ${SENTRY_TAG} FastAPI + ASGI integration ──"
    if [ "$OFFLINE" = "1" ]; then
        verify_at_tag /tmp/sentry-python "$SENTRY_TAG"
    else
        if [ ! -d /tmp/sentry-python/.git ]; then
            rm -rf /tmp/sentry-python
            git clone https://github.com/getsentry/sentry-python /tmp/sentry-python
        fi
        git -C /tmp/sentry-python fetch --tags --force --depth 1 origin "$SENTRY_TAG"
        git -C /tmp/sentry-python reset --hard "$SENTRY_TAG"
        git -C /tmp/sentry-python clean -fdx \
            -- ':!tests/integrations/fastapi/conftest.py' \
               ':!tests/integrations/asgi/conftest.py'
        "$PYTHON_BIN" -m pip install -q "sentry-sdk[fastapi]==${SENTRY_TAG}"
    fi

    for tree in fastapi asgi; do
        cat > /tmp/sentry-python/tests/integrations/$tree/conftest.py <<'PY'
import fastapi_turbo  # noqa: F401
PY
    done

    "$PYTHON_BIN" -m pytest \
        /tmp/sentry-python/tests/integrations/fastapi \
        /tmp/sentry-python/tests/integrations/asgi \
        -q --tb=short
}

case "$GATE" in
    fastapi) run_fastapi_gate ;;
    sentry)  run_sentry_gate ;;
    all)     run_fastapi_gate; run_sentry_gate ;;
    *)
        echo "Usage: $0 [all|fastapi|sentry]" >&2
        exit 2
        ;;
esac

echo "── External compat gates green for the requested target. ──"
