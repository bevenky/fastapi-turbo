"""R24 audit follow-ups — ``FormData.close`` recognises pre-shim
Starlette ``UploadFile``, ``PyUploadFile`` coroutine wrappers carry
upstream-shaped ``__name__`` / ``__qualname__`` / signatures, and
the sandbox-detection conftest honours
``FASTAPI_TURBO_FORCE_LOOPBACK_DENIED`` / ``…_ALLOWED`` overrides."""
import asyncio
import inspect

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 FormData.close recognises pre-shim Starlette UploadFile
# ────────────────────────────────────────────────────────────────────


def test_formdata_close_handles_pre_shim_starlette_uploadfile():
    """If user code imported ``starlette.datastructures.UploadFile``
    BEFORE the fastapi_turbo shim ran (or ``starlette`` got
    re-imported after the shim was disabled), the resulting class
    is a genuinely-different type than ``fastapi_turbo.UploadFile``.
    Our previous strict-isinstance check missed that case and left
    pre-shim uploads open. ``FormData.close`` now recognises both."""
    import sys
    from fastapi_turbo.datastructures import FormData

    # Synthesise a "pre-shim" Starlette ``UploadFile`` — register a
    # fake module under ``starlette.datastructures`` whose
    # ``UploadFile`` is a separate type from our shimmed one. Restore
    # the prior module on exit so we don't poison other tests.
    import types
    original_mod = sys.modules.get("starlette.datastructures")
    try:
        fake_mod = types.ModuleType("starlette.datastructures")
        closed_log: list[bool] = []

        class _StarletteUF:
            """Stand-in for ``starlette.datastructures.UploadFile``."""

            def __init__(self):
                self.filename = "x.txt"
                self.file = None

            async def close(self):
                closed_log.append(True)

        fake_mod.UploadFile = _StarletteUF
        sys.modules["starlette.datastructures"] = fake_mod

        async def _run() -> list[bool]:
            f = FormData([("file", _StarletteUF())])
            await f.close()
            return closed_log

        result = asyncio.run(_run())
        assert result == [True], result
    finally:
        if original_mod is not None:
            sys.modules["starlette.datastructures"] = original_mod
        else:
            sys.modules.pop("starlette.datastructures", None)


def test_formdata_close_still_strict_when_starlette_module_absent():
    """If ``starlette.datastructures`` isn't in ``sys.modules`` (or
    its ``UploadFile`` symbol is missing), ``FormData.close`` falls
    back to the pure ``fastapi_turbo.UploadFile`` check — no
    ``AttributeError`` from probing a missing class. Regression
    guard: the lookup must not crash on minimal-environment runs."""
    import sys
    from fastapi_turbo.datastructures import FormData

    saved = sys.modules.pop("starlette.datastructures", None)
    try:
        async def _run() -> bool:
            f = FormData([("a", "1")])
            await f.close()
            return True

        assert asyncio.run(_run()) is True
    finally:
        if saved is not None:
            sys.modules["starlette.datastructures"] = saved


# ────────────────────────────────────────────────────────────────────
# #2 PyUploadFile coroutine wrappers carry upstream metadata
# ────────────────────────────────────────────────────────────────────


def test_pyuploadfile_method_metadata_matches_upstream_shape():
    """``UploadFile.read.__name__`` is ``"read"``,
    ``__qualname__`` is ``"UploadFile.read"`` (matching Starlette's
    class-bound shape). Earlier the wrappers exposed
    ``_async_upload_read`` for both, which surfaced in debugger
    traces and any tool that introspects ``__qualname__``."""
    pytest.importorskip("fastapi_turbo._fastapi_turbo_core")
    from fastapi_turbo._fastapi_turbo_core import PyUploadFile

    expected = {
        "read": ("read", "UploadFile.read"),
        "write": ("write", "UploadFile.write"),
        "seek": ("seek", "UploadFile.seek"),
        "close": ("close", "UploadFile.close"),
    }
    for attr, (name, qualname) in expected.items():
        m = getattr(PyUploadFile, attr)
        assert m.__name__ == name, (attr, m.__name__, name)
        assert m.__qualname__ == qualname, (attr, m.__qualname__, qualname)


def test_pyuploadfile_signatures_have_starlette_shape():
    """``inspect.signature`` returns the same parameter names /
    defaults / annotations a caller would get from upstream
    Starlette: ``read(self, size: int = -1) -> bytes`` etc."""
    from fastapi_turbo._fastapi_turbo_core import PyUploadFile

    sig_read = inspect.signature(PyUploadFile.read)
    params_read = list(sig_read.parameters.values())
    assert [p.name for p in params_read] == ["self", "size"], sig_read
    assert params_read[1].default == -1, sig_read
    assert params_read[1].annotation is int, sig_read
    assert sig_read.return_annotation is bytes, sig_read

    sig_write = inspect.signature(PyUploadFile.write)
    params_write = list(sig_write.parameters.values())
    assert [p.name for p in params_write] == ["self", "data"], sig_write
    assert params_write[1].annotation is bytes, sig_write
    assert sig_write.return_annotation is None, sig_write


# ────────────────────────────────────────────────────────────────────
# #3 sandbox-detection env-var overrides
# ────────────────────────────────────────────────────────────────────


def _load_conftest_module():
    """Load ``tests/conftest.py`` directly (it's not on a package
    path, so ``import tests.conftest`` doesn't work). Returns the
    loaded module so callers can introspect / call its helpers."""
    import importlib.util
    import pathlib

    here = pathlib.Path(__file__).resolve().parent
    conftest_path = here.parent / "conftest.py"
    spec = importlib.util.spec_from_file_location(
        "_r24_conftest_probe", str(conftest_path)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_loopback_denied_helper_honours_force_denied(monkeypatch):
    """``FASTAPI_TURBO_FORCE_LOOPBACK_DENIED=1`` forces
    ``LOOPBACK_DENIED = True`` regardless of what
    ``socket.bind(('127.0.0.1', 0))`` actually does. Auditors / CI
    use this to exercise the ``requires_loopback`` skip path on a
    dev box that *can* bind."""
    monkeypatch.setenv("FASTAPI_TURBO_FORCE_LOOPBACK_DENIED", "1")
    monkeypatch.delenv("FASTAPI_TURBO_FORCE_LOOPBACK_ALLOWED", raising=False)

    mod = _load_conftest_module()
    assert mod._loopback_denied() is True


def test_loopback_denied_helper_honours_force_allowed(monkeypatch):
    """``FASTAPI_TURBO_FORCE_LOOPBACK_ALLOWED=1`` flips the helper to
    ``False`` even if the actual probe would fail (sandbox
    environments where bind probe-fails-but-real-server-works)."""
    monkeypatch.setenv("FASTAPI_TURBO_FORCE_LOOPBACK_ALLOWED", "1")
    monkeypatch.delenv("FASTAPI_TURBO_FORCE_LOOPBACK_DENIED", raising=False)

    mod = _load_conftest_module()
    # Force the underlying probe to fail; with the override env
    # var, ``_loopback_denied`` must still return False.
    monkeypatch.setattr(mod, "_can_bind_loopback", lambda: False)
    assert mod._loopback_denied() is False
