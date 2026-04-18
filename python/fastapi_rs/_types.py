"""Type aliases matching ``fastapi.types``.

These are used by third-party libraries that import from ``fastapi.types``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Set, Union

DecoratedCallable = Any  # TypeVar in real FastAPI, but Any is compatible
IncEx = Union[Set[int], Set[str], Dict[int, Any], Dict[str, Any], None]
DependencyCacheKey = tuple  # tuple[Callable[..., Any], tuple[str, ...]]
ModelNameMap = dict  # dict[type[Any], str]
