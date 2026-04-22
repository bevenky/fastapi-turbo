"""psycopg3 sync SQLAlchemy parity app. Sets SQLA_SUFFIX=pg3 so tables are isolated."""
import os
os.environ.setdefault("SQLA_SUFFIX", "pg3")

# Each server gets its OWN database (set via SQLA_URL_PG3) so FA and FR can be
# compared independently without cross-contaminating state.
_DEFAULT_URL = "postgresql+psycopg://venky@localhost:5432/jamun_sqla_pg3_fa"
DB_URL = os.environ.get("SQLA_URL_PG3", _DEFAULT_URL)

from tests.parity.sqla_sync_app import build_app  # noqa: E402
app = build_app(DB_URL)
