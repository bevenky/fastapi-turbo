"""asyncpg async SQLAlchemy parity app. Sets SQLA_SUFFIX=async."""
import os
os.environ.setdefault("SQLA_SUFFIX", "async")

_DEFAULT_URL = "postgresql+asyncpg://venky@localhost:5432/fastapi_turbo_sqla_async_fa"
DB_URL = os.environ.get("SQLA_URL_ASYNC", _DEFAULT_URL)

from tests.parity.sqla_async_app import build_app  # noqa: E402
app = build_app(DB_URL)
