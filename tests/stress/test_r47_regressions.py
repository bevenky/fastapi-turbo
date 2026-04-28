"""R47 audit follow-up — WebSocket dispatcher honours
``dependency_overrides``. Net sandboxed-gate change: 111 → 110
failed.
"""
import pytest

import fastapi_turbo  # noqa: F401


def test_ws_dependency_overrides_replaces_dep_callable():
    """``app.dependency_overrides`` is a public escape hatch for
    swapping in test doubles. The HTTP path honoured it; the WS
    dispatcher used to call the original callable directly,
    breaking upstream's ``test_router_ws_depends_with_override``."""
    from fastapi_turbo import Depends, FastAPI
    from fastapi_turbo.testclient import TestClient

    def real_dep():
        return "real"

    app = FastAPI()

    @app.websocket("/ws")
    async def ws_handler(websocket, value: str = Depends(real_dep)):
        await websocket.accept()
        await websocket.send_text(value)
        await websocket.close()

    app.dependency_overrides[real_dep] = lambda: "override"

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_text() == "override"


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
