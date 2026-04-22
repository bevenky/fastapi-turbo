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

    def __init__(self, dependency=None, *, use_cache: bool = True, scope: str | None = None):
        self.dependency = dependency
        self.use_cache = use_cache
        self.scope = scope

    def __repr__(self) -> str:
        return f"Depends({self.dependency!r}, use_cache={self.use_cache})"

    def __hash__(self) -> int:
        # FA parity (FA 0.122+): ``Depends`` is hashable so tools like
        # SQLAlchemy's ``registry.mapped`` can put markers in sets/dicts.
        # Equality is value-based: same dependency + use_cache + scope.
        return hash((type(self), self.dependency, self.use_cache, self.scope))

    def __eq__(self, other) -> bool:
        if not isinstance(other, Depends):
            return NotImplemented
        return (
            type(self) is type(other)
            and self.dependency is other.dependency
            and self.use_cache == other.use_cache
            and self.scope == other.scope
        )


class Security(Depends):
    """Security dependency — a Depends() variant that carries OAuth2 scopes.

    Usage::

        from fastapi_rs import Security
        from fastapi_rs.security import OAuth2PasswordBearer

        oauth2 = OAuth2PasswordBearer(tokenUrl="token")

        @app.get("/me")
        async def me(token: str = Security(oauth2, scopes=["me"])):
            ...

    Matches FastAPI's ``fastapi.Security`` class exactly.
    """

    def __init__(self, dependency=None, *, scopes=None, use_cache: bool = True):
        super().__init__(dependency, use_cache=use_cache)
        self.scopes = list(scopes) if scopes else []

    def __repr__(self) -> str:
        return f"Security({self.dependency!r}, scopes={self.scopes!r}, use_cache={self.use_cache})"

    def __hash__(self) -> int:
        return hash((
            type(self), self.dependency, self.use_cache, self.scope,
            tuple(self.scopes),
        ))

    def __eq__(self, other) -> bool:
        if not isinstance(other, Security):
            return NotImplemented
        return (
            type(self) is type(other)
            and self.dependency is other.dependency
            and self.use_cache == other.use_cache
            and self.scope == other.scope
            and self.scopes == other.scopes
        )
