"""Dependency injection marker, matching FastAPI's Depends interface."""

from __future__ import annotations


class Depends:
    """Declares a dependency that will be resolved at request time.

    Usage::

        def get_db():
            return Database()

        @app.get("/items")
        def list_items(db=Depends(get_db)):
            ...

    Parameters
    ----------
    dependency : callable, optional
        The function (sync or async) to call to produce the injected value.
    use_cache : bool
        If True (default), the same dependency callable used multiple times
        within a single request will only be called once and the result reused.
    """

    def __init__(self, dependency=None, *, use_cache: bool = True):
        self.dependency = dependency
        self.use_cache = use_cache

    def __repr__(self) -> str:
        return f"Depends({self.dependency!r}, use_cache={self.use_cache})"
