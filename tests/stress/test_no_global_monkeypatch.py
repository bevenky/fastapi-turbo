"""Regression: importing fastapi_turbo must not mutate unrelated
third-party modules.

Previously ``fastapi_turbo/__init__.py`` patched
``psycopg_pool.ConnectionPool.__init__`` to default ``autocommit=True``
for every pool in the process. That's a silent behavioural change for
any code that just happens to have ``import fastapi_turbo`` in its
startup path, even if it never uses the framework.
"""
from __future__ import annotations

import pytest


def test_importing_fastapi_turbo_leaves_psycopg_pool_untouched():
    pp = pytest.importorskip("psycopg_pool")

    orig_init = pp.ConnectionPool.__init__

    import fastapi_turbo  # noqa: F401  — the act of importing is the trigger

    assert pp.ConnectionPool.__init__ is orig_init, (
        "ConnectionPool.__init__ was monkey-patched by fastapi_turbo import"
    )


def test_create_pool_opt_in_exists():
    """The opt-in ``create_pool`` helper is still exported under
    ``fastapi_turbo.db`` — users can continue to construct
    autocommit-enabled pools through it without affecting other code."""
    pytest.importorskip("psycopg_pool")
    from fastapi_turbo.db import create_pool

    assert callable(create_pool)
