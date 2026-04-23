"""Boot one parity SQLA app (specified via CLI) and seed a user row.

Usage:
    sqla_runner.py {pg3|pg2|async} {fastapi-rs|uvicorn} PORT
"""
import os
import sys

mode, stack, port = sys.argv[1], sys.argv[2], int(sys.argv[3])

# Route to the RS-owned DB (_fr suffix) so table names match parity apps.
DB_URLS = {
    "pg3":       "postgresql+psycopg://venky@localhost:5432/jamun_sqla_pg3_fr",
    "pg2":       "postgresql+psycopg2://venky@localhost:5432/jamun_sqla_pg2_fr",
    "async":     "postgresql+asyncpg://venky@localhost:5432/jamun_sqla_async_fr",
    "pg3async":  "postgresql+psycopg://venky@localhost:5432/jamun_sqla_pg3async_fr",
}
# Parity apps' table-suffix follows SQLA_SUFFIX; pg3async reuses "async"
# since the async ORM code path is identical (different driver only).
_SQLA_SUFFIX = {"pg3async": "async"}.get(mode, mode)
os.environ["SQLA_SUFFIX"] = _SQLA_SUFFIX

# If running under fastapi-rs: install the compat shim so the parity app's
# ``from fastapi import ...`` imports resolve to fastapi-rs transparently.
if stack == "fastapi-rs":
    from fastapi_rs.compat import install
    install()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".."))

if mode in ("async", "pg3async"):
    from tests.parity.sqla_async_app import build_app
else:
    from tests.parity.sqla_sync_app import build_app

app = build_app(DB_URLS[mode])

# Strip the parity app's startup handlers (drop_all / create_all). The tables
# exist already from a prior parity run; re-running drop_all under the bench
# harness's worker loop hangs because SQLAlchemy's greenlet bridge on a
# cold engine contends with the same loop that's about to serve requests.
# fastapi-rs exposes ``_on_startup``; stock FastAPI exposes it on ``.router``
# as ``on_startup`` (plain list). Handle both.
if mode in ("async", "pg3async"):
    if hasattr(app, "_on_startup"):
        app._on_startup.clear()
        app._on_shutdown.clear()
    elif hasattr(app.router, "on_startup"):
        app.router.on_startup.clear()
        app.router.on_shutdown.clear()

# Seed a user so GET /users/1 returns data (harmless if already seeded).
import httpx
try:
    pass  # seeding happens via first POST the runner issues
except Exception:
    pass

if stack == "fastapi-rs":
    app.run(host="127.0.0.1", port=port)
else:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
