"""psycopg2 sync SQLAlchemy parity app. Sets SQLA_SUFFIX=pg2."""
import os
os.environ.setdefault("SQLA_SUFFIX", "pg2")

_DEFAULT_URL = "postgresql+psycopg2://venky@localhost:5432/jamun_sqla_pg2_fa"
DB_URL = os.environ.get("SQLA_URL_PG2", _DEFAULT_URL)

from tests.parity.sqla_sync_app import build_app  # noqa: E402
app = build_app(DB_URL)
