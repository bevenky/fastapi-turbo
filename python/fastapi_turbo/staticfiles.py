"""Static file serving — re-exported from REAL ``starlette.staticfiles``.

The hand-written clone ``StaticFiles`` (its own ``lookup_path`` returning
``(full_path, media_type)`` plus a hand-rolled ASGI ``__call__``) is retired.
Nothing in the engine depends on the clone-specific surface:

  * The Rust door serves mounted static dirs through tower-http's ``ServeDir``
    (``src/server.rs::CachedServeDir``). ``applications.py`` collects the mount
    by reading ``mounted_app.directory`` off the instance — real Starlette
    ``StaticFiles`` stores exactly that attribute, so the door mount is
    byte-for-byte unchanged (verified end-to-end by the ``/static`` parity gate
    and ``tests/test_files.py::test_static_files_serves_file``).
  * TestClient / ``app.run`` static requests take the same ``ServeDir`` path;
    there is no in-process ASGI dispatcher that leans on the clone's ``__call__``.

Contract note: real Starlette ``lookup_path(path)`` returns
``(full_path, os.stat_result | None)`` — NOT ``(full_path, media_type)`` like
the clone. The media type is resolved later in ``get_response``. Traversal /
sibling-prefix escapes are rejected by real Starlette's own
``os.path.commonpath`` check (the same guard the clone copied), returning
``("", None)``.

Imported during ``fastapi_turbo`` package init BEFORE the compat shim rebinds
``sys.modules``, so ``starlette.staticfiles`` resolves to the REAL package; the
bound reference stays real after the shim runs.
"""

from starlette.staticfiles import StaticFiles

__all__ = ["StaticFiles"]
