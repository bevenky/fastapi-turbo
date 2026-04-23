"""Parity app 3: patterns 251-400 (WebSocket, SSE/Streaming, File Handling, Security).

This app uses ONLY stock FastAPI imports so it runs identically on:
  - FastAPI + uvicorn  (port 29300)
  - fastapi-turbo         (port 29301)
"""

import asyncio
import base64
import io
import json
import os
import tempfile
import time

from fastapi import (
    FastAPI,
    WebSocket,
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    UploadFile,
    Request,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.security import (
    APIKeyHeader,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    HTTPAuthorizationCredentials,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    SecurityScopes,
)

app = FastAPI()

# ── Shared fixtures ──────────────────────────────────────────────────

# Create a temp file for FileResponse tests
_TMPDIR = tempfile.mkdtemp(prefix="parity3_")
_SAMPLE_TXT = os.path.join(_TMPDIR, "sample.txt")
_SAMPLE_BIN = os.path.join(_TMPDIR, "sample.bin")
_SAMPLE_HTML = os.path.join(_TMPDIR, "sample.html")
_SAMPLE_PNG = os.path.join(_TMPDIR, "sample.png")
_LARGE_FILE = os.path.join(_TMPDIR, "large.dat")

with open(_SAMPLE_TXT, "w") as f:
    f.write("Hello parity test\nLine 2\nLine 3\n")
with open(_SAMPLE_BIN, "wb") as f:
    f.write(bytes(range(256)) * 4)
with open(_SAMPLE_HTML, "w") as f:
    f.write("<html><body><h1>Test</h1></body></html>")
with open(_SAMPLE_PNG, "wb") as f:
    # Minimal PNG-like header for mime detection
    f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
with open(_LARGE_FILE, "wb") as f:
    f.write(b"X" * (1024 * 1024))  # 1 MB


# ═══════════════════════════════════════════════════════════════════════
# PATTERNS 251-270: WebSocket
# ═══════════════════════════════════════════════════════════════════════


# p251: Basic WS echo text
@app.websocket("/ws/p251")
async def ws_p251(websocket: WebSocket):
    await websocket.accept()
    data = await websocket.receive_text()
    await websocket.send_text(f"echo:{data}")
    await websocket.close()


# p252: WS echo binary
@app.websocket("/ws/p252")
async def ws_p252(websocket: WebSocket):
    await websocket.accept()
    data = await websocket.receive_bytes()
    await websocket.send_bytes(data)
    await websocket.close()


# p253: WS echo JSON (send_json/receive_json)
@app.websocket("/ws/p253")
async def ws_p253(websocket: WebSocket):
    await websocket.accept()
    data = await websocket.receive_json()
    data["echoed"] = True
    await websocket.send_json(data)
    await websocket.close()


# p254: WS multiple messages in sequence
@app.websocket("/ws/p254")
async def ws_p254(websocket: WebSocket):
    await websocket.accept()
    for _ in range(5):
        msg = await websocket.receive_text()
        await websocket.send_text(f"got:{msg}")
    await websocket.close()


# p255: WS accept with subprotocol
@app.websocket("/ws/p255")
async def ws_p255(websocket: WebSocket):
    await websocket.accept(subprotocol="chat.v1")
    await websocket.send_text("subproto-ok")
    await websocket.close()


# p256: WS close with code
@app.websocket("/ws/p256")
async def ws_p256(websocket: WebSocket):
    await websocket.accept()
    await websocket.close(code=4001)


# p257: WS close with reason
@app.websocket("/ws/p257")
async def ws_p257(websocket: WebSocket):
    await websocket.accept()
    await websocket.close(code=4002, reason="custom-reason")


# p258: WS query params
@app.websocket("/ws/p258")
async def ws_p258(websocket: WebSocket):
    await websocket.accept()
    token = websocket.query_params.get("token", "none")
    await websocket.send_text(f"token:{token}")
    await websocket.close()


# p259: WS path params
@app.websocket("/ws/p259/{room_id}")
async def ws_p259(websocket: WebSocket, room_id: str):
    await websocket.accept()
    await websocket.send_text(f"room:{room_id}")
    await websocket.close()


# p260: WS reject (close before accept)
@app.websocket("/ws/p260")
async def ws_p260(websocket: WebSocket):
    # Do NOT accept -- just return. Connection should be refused/closed.
    return


# p261: WS send after close (should error)
@app.websocket("/ws/p261")
async def ws_p261(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("before-close")
    await websocket.close()
    try:
        await websocket.send_text("after-close")
        result = "NO_ERROR"
    except Exception as e:
        result = f"ERROR:{type(e).__name__}"
    # Can't send result since connection is closed, but at least no crash


# p262: WS with custom param name (not "websocket")
@app.websocket("/ws/p262")
async def ws_p262(ws: WebSocket):
    await ws.accept()
    msg = await ws.receive_text()
    await ws.send_text(f"custom-param:{msg}")
    await ws.close()


# p263: WS on APIRouter
ws_router = APIRouter(prefix="/wsrouter")


@ws_router.websocket("/p263")
async def ws_p263(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("from-router")
    await websocket.close()


app.include_router(ws_router)


# p264: WS on nested router
outer_ws_router = APIRouter(prefix="/outer")
inner_ws_router = APIRouter(prefix="/inner")


@inner_ws_router.websocket("/p264")
async def ws_p264(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("nested-router")
    await websocket.close()


outer_ws_router.include_router(inner_ws_router)
app.include_router(outer_ws_router)


# p265: WS iter_text
@app.websocket("/ws/p265")
async def ws_p265(websocket: WebSocket):
    await websocket.accept()
    collected = []
    async for msg in websocket.iter_text():
        collected.append(msg)
        if msg == "STOP":
            break
    # Send count before close
    await websocket.send_text(f"count:{len(collected)}")
    await websocket.close()


# p266: WS large message
@app.websocket("/ws/p266")
async def ws_p266(websocket: WebSocket):
    await websocket.accept()
    data = await websocket.receive_text()
    await websocket.send_text(f"len:{len(data)}")
    await websocket.close()


# p267: WS binary roundtrip exact bytes
@app.websocket("/ws/p267")
async def ws_p267(websocket: WebSocket):
    await websocket.accept()
    data = await websocket.receive_bytes()
    # Echo back reversed
    await websocket.send_bytes(data[::-1])
    await websocket.close()


# p268: WS send_json mode=binary
@app.websocket("/ws/p268")
async def ws_p268(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"mode": "binary"}, mode="binary")
    await websocket.close()


# p269: WS receive dict (low-level ASGI)
@app.websocket("/ws/p269")
async def ws_p269(websocket: WebSocket):
    await websocket.accept()
    msg = await websocket.receive()
    msg_type = msg.get("type", "unknown")
    has_text = "text" in msg
    await websocket.send_text(f"type:{msg_type},has_text:{has_text}")
    await websocket.close()


# p270: WS headers accessible
@app.websocket("/ws/p270")
async def ws_p270(websocket: WebSocket):
    await websocket.accept()
    ua = websocket.headers.get("user-agent", "none")
    await websocket.send_text(f"ua:{ua}")
    await websocket.close()


# ═══════════════════════════════════════════════════════════════════════
# PATTERNS 271-320: SSE / Streaming
# ═══════════════════════════════════════════════════════════════════════


# p271: StreamingResponse text/plain sync generator
@app.get("/p271")
def handler_p271():
    def gen():
        for i in range(5):
            yield f"chunk-{i}\n"
    return StreamingResponse(gen(), media_type="text/plain")


# p272: StreamingResponse async generator
@app.get("/p272")
async def handler_p272():
    async def gen():
        for i in range(5):
            yield f"async-chunk-{i}\n"
    return StreamingResponse(gen(), media_type="text/plain")


# p273: StreamingResponse text/event-stream (SSE)
@app.get("/p273")
async def handler_p273():
    async def gen():
        for i in range(3):
            yield f"data: event-{i}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p274: SSE with data: lines
@app.get("/p274")
async def handler_p274():
    async def gen():
        yield "data: hello\n\n"
        yield "data: world\n\n"
        yield "data: done\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p275: SSE with [DONE] terminator
@app.get("/p275")
async def handler_p275():
    async def gen():
        yield "data: chunk1\n\n"
        yield "data: chunk2\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p276: SSE mixing str + bytes yields
@app.get("/p276")
async def handler_p276():
    async def gen():
        yield "data: string\n\n"
        yield b"data: bytes\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p277: SSE content-type header correct
@app.get("/p277")
async def handler_p277():
    async def gen():
        yield "data: check-ct\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p278: EventSourceResponse
@app.get("/p278")
async def handler_p278():
    from fastapi.responses import StreamingResponse as SR
    async def gen():
        yield "data: sse1\n\n"
        yield "data: sse2\n\n"
    # EventSourceResponse is StreamingResponse with text/event-stream
    return SR(gen(), media_type="text/event-stream",
              headers={"Cache-Control": "no-store"})


# p279: Large streaming (1000 chunks)
@app.get("/p279")
async def handler_p279():
    async def gen():
        for i in range(1000):
            yield f"{i}\n"
    return StreamingResponse(gen(), media_type="text/plain")


# p280: Streaming with custom headers
@app.get("/p280")
async def handler_p280():
    def gen():
        yield "custom-header-data\n"
    return StreamingResponse(
        gen(), media_type="text/plain",
        headers={"X-Custom-Stream": "true", "X-Count": "1"},
    )


# p281: StreamingResponse status_code=200
@app.get("/p281")
async def handler_p281():
    def gen():
        yield "ok\n"
    return StreamingResponse(gen(), status_code=200, media_type="text/plain")


# p282: StreamingResponse from list iterator
@app.get("/p282")
async def handler_p282():
    chunks = ["a", "b", "c"]
    return StreamingResponse(iter(chunks), media_type="text/plain")


# p283: StreamingResponse from file-like object
@app.get("/p283")
async def handler_p283():
    buf = io.BytesIO(b"file-like-content-here")
    return StreamingResponse(buf, media_type="application/octet-stream")


# p284: Streaming empty generator
@app.get("/p284")
async def handler_p284():
    async def gen():
        return
        yield  # noqa -- make it a generator
    return StreamingResponse(gen(), media_type="text/plain")


# p285: Streaming single chunk
@app.get("/p285")
async def handler_p285():
    def gen():
        yield "only-one"
    return StreamingResponse(gen(), media_type="text/plain")


# p286: Streaming bytes generator
@app.get("/p286")
async def handler_p286():
    def gen():
        for i in range(3):
            yield f"bytes-{i}\n".encode()
    return StreamingResponse(gen(), media_type="application/octet-stream")


# p287: Streaming with 206 status
@app.get("/p287")
async def handler_p287():
    def gen():
        yield "partial"
    return StreamingResponse(gen(), status_code=206, media_type="text/plain")


# p288: Streaming unicode content
@app.get("/p288")
async def handler_p288():
    async def gen():
        yield "hello "
        yield "world "
        yield "\u00e9\u00e0\u00fc"
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


# p289: SSE with event type field
@app.get("/p289")
async def handler_p289():
    async def gen():
        yield "event: update\ndata: payload1\n\n"
        yield "event: done\ndata: payload2\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p290: SSE with id field
@app.get("/p290")
async def handler_p290():
    async def gen():
        yield "id: 1\ndata: first\n\n"
        yield "id: 2\ndata: second\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p291: SSE multi-line data
@app.get("/p291")
async def handler_p291():
    async def gen():
        yield "data: line1\ndata: line2\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p292: SSE with retry field
@app.get("/p292")
async def handler_p292():
    async def gen():
        yield "retry: 3000\ndata: retry-set\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p293: SSE with comment
@app.get("/p293")
async def handler_p293():
    async def gen():
        yield ": this is a comment\ndata: after-comment\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p294: SSE JSON data
@app.get("/p294")
async def handler_p294():
    async def gen():
        yield f'data: {json.dumps({"key": "value"})}\n\n'
    return StreamingResponse(gen(), media_type="text/event-stream")


# p295: SSE multiple events rapid
@app.get("/p295")
async def handler_p295():
    async def gen():
        for i in range(10):
            yield f"data: rapid-{i}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p296: SSE with empty data field
@app.get("/p296")
async def handler_p296():
    async def gen():
        yield "data: \n\n"
        yield "data: notempty\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p297: SSE newline-only data
@app.get("/p297")
async def handler_p297():
    async def gen():
        yield "data:\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p298: SSE combined event+id+data
@app.get("/p298")
async def handler_p298():
    async def gen():
        yield "event: msg\nid: 42\ndata: combined\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p299: SSE keep-alive comment
@app.get("/p299")
async def handler_p299():
    async def gen():
        yield ": keepalive\n\n"
        yield "data: after-keepalive\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p300: SSE with numeric data
@app.get("/p300")
async def handler_p300():
    async def gen():
        yield "data: 12345\n\n"
        yield "data: 67890\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# p301-p310: Streaming + middleware interaction

# p301: Streaming response alongside JSON endpoint
@app.get("/p301/json")
async def handler_p301_json():
    return {"source": "json"}

@app.get("/p301/stream")
async def handler_p301_stream():
    def gen():
        yield "streamed"
    return StreamingResponse(gen(), media_type="text/plain")


# p302: Streaming with content-length absent (chunked)
@app.get("/p302")
async def handler_p302():
    def gen():
        yield "no-content-length"
    return StreamingResponse(gen(), media_type="text/plain")


# p303: Streaming 201 status
@app.get("/p303")
async def handler_p303():
    def gen():
        yield "created"
    return StreamingResponse(gen(), status_code=201, media_type="text/plain")


# p304: Streaming with multiple custom headers
@app.get("/p304")
async def handler_p304():
    def gen():
        yield "multi-header"
    return StreamingResponse(
        gen(), media_type="text/plain",
        headers={"X-A": "1", "X-B": "2", "X-C": "3"},
    )


# p305: Streaming preserves media_type in header
@app.get("/p305")
async def handler_p305():
    def gen():
        yield "<html><body>streamed</body></html>"
    return StreamingResponse(gen(), media_type="text/html")


# p306: Streaming + POST method
@app.post("/p306")
async def handler_p306():
    def gen():
        yield "post-stream"
    return StreamingResponse(gen(), media_type="text/plain")


# p307: Streaming with empty string chunk
@app.get("/p307")
async def handler_p307():
    def gen():
        yield ""
        yield "after-empty"
    return StreamingResponse(gen(), media_type="text/plain")


# p308: Streaming response with JSON media type
@app.get("/p308")
async def handler_p308():
    def gen():
        yield '{"streaming":'
        yield '"json"}'
    return StreamingResponse(gen(), media_type="application/json")


# p309: Multiple streaming endpoints
@app.get("/p309/a")
async def handler_p309a():
    def gen():
        yield "stream-a"
    return StreamingResponse(gen(), media_type="text/plain")

@app.get("/p309/b")
async def handler_p309b():
    def gen():
        yield "stream-b"
    return StreamingResponse(gen(), media_type="text/plain")


# p310: Streaming binary with exact bytes
@app.get("/p310")
async def handler_p310():
    def gen():
        yield bytes([0, 1, 2, 3, 255, 254, 253])
    return StreamingResponse(gen(), media_type="application/octet-stream")


# p311-p320: Streaming + error handling

# p311: Streaming 404
@app.get("/p311")
async def handler_p311():
    def gen():
        yield "not found content"
    return StreamingResponse(gen(), status_code=404, media_type="text/plain")


# p312: Streaming 500
@app.get("/p312")
async def handler_p312():
    def gen():
        yield "error content"
    return StreamingResponse(gen(), status_code=500, media_type="text/plain")


# p313: Streaming 204 (no content but with stream)
@app.get("/p313")
async def handler_p313():
    def gen():
        return
        yield
    return StreamingResponse(gen(), status_code=204, media_type="text/plain")


# p314: Streaming with various status codes
@app.get("/p314/{code}")
async def handler_p314(code: int):
    def gen():
        yield f"status-{code}"
    return StreamingResponse(gen(), status_code=code, media_type="text/plain")


# p315: Streaming async generator with delay
@app.get("/p315")
async def handler_p315():
    async def gen():
        yield "start\n"
        await asyncio.sleep(0.01)
        yield "end\n"
    return StreamingResponse(gen(), media_type="text/plain")


# p316: Streaming response preserves order
@app.get("/p316")
async def handler_p316():
    def gen():
        for i in range(20):
            yield f"{i},"
    return StreamingResponse(gen(), media_type="text/plain")


# p317: Streaming all bytes 0-255
@app.get("/p317")
async def handler_p317():
    def gen():
        yield bytes(range(256))
    return StreamingResponse(gen(), media_type="application/octet-stream")


# p318: Streaming text with special chars
@app.get("/p318")
async def handler_p318():
    def gen():
        yield 'line1\nline2\ttab\r\nCRLF'
    return StreamingResponse(gen(), media_type="text/plain")


# p319: Streaming with query param
@app.get("/p319")
async def handler_p319(n: int = 3):
    def gen():
        for i in range(n):
            yield f"item-{i}\n"
    return StreamingResponse(gen(), media_type="text/plain")


# p320: Streaming + path param
@app.get("/p320/{name}")
async def handler_p320(name: str):
    def gen():
        yield f"hello-{name}"
    return StreamingResponse(gen(), media_type="text/plain")


# ═══════════════════════════════════════════════════════════════════════
# PATTERNS 321-370: File Handling
# ═══════════════════════════════════════════════════════════════════════


# p321: UploadFile single file
@app.post("/p321")
async def handler_p321(file: UploadFile):
    data = await file.read()
    return {"size": len(data)}


# p322: UploadFile with filename
@app.post("/p322")
async def handler_p322(file: UploadFile):
    return {"filename": file.filename}


# p323: UploadFile with content_type
@app.post("/p323")
async def handler_p323(file: UploadFile):
    return {"content_type": file.content_type}


# p324: UploadFile read() returns bytes
@app.post("/p324")
async def handler_p324(file: UploadFile):
    data = await file.read()
    return {"is_bytes": isinstance(data, bytes), "hex": data[:4].hex()}


# p325: UploadFile size attribute
@app.post("/p325")
async def handler_p325(file: UploadFile):
    data = await file.read()
    return {"read_len": len(data)}


# p326: Multiple file upload
@app.post("/p326")
async def handler_p326(files: list[UploadFile]):
    names = [f.filename for f in files]
    return {"count": len(files), "names": names}


# p327: File + Form combined
@app.post("/p327")
async def handler_p327(name: str = Form(), file: UploadFile = File()):
    data = await file.read()
    return {"name": name, "filename": file.filename, "size": len(data)}


# p328: Large file upload (1MB)
@app.post("/p328")
async def handler_p328(file: UploadFile):
    data = await file.read()
    return {"size": len(data)}


# p329: FileResponse basic
@app.get("/p329")
async def handler_p329():
    return FileResponse(_SAMPLE_TXT)


# p330: FileResponse with filename (content-disposition)
@app.get("/p330")
async def handler_p330():
    return FileResponse(_SAMPLE_TXT, filename="download.txt")


# p331: FileResponse with content_disposition_type="inline"
@app.get("/p331")
async def handler_p331():
    return FileResponse(_SAMPLE_TXT, filename="view.txt",
                        content_disposition_type="inline")


# p332: FileResponse auto media_type from extension
@app.get("/p332")
async def handler_p332():
    return FileResponse(_SAMPLE_HTML)


# p333: FileResponse custom media_type
@app.get("/p333")
async def handler_p333():
    return FileResponse(_SAMPLE_TXT, media_type="application/octet-stream")


# p334: Upload empty file
@app.post("/p334")
async def handler_p334(file: UploadFile):
    data = await file.read()
    return {"size": len(data), "filename": file.filename}


# p335: Upload file with special chars in name
@app.post("/p335")
async def handler_p335(file: UploadFile):
    return {"filename": file.filename}


# p336: Upload text file and verify content
@app.post("/p336")
async def handler_p336(file: UploadFile):
    data = await file.read()
    return {"content": data.decode("utf-8")}


# p337: Upload binary file with specific bytes
@app.post("/p337")
async def handler_p337(file: UploadFile):
    data = await file.read()
    return {"hex": data.hex()}


# p338: Upload with form field before file
@app.post("/p338")
async def handler_p338(description: str = Form(), file: UploadFile = File()):
    data = await file.read()
    return {"description": description, "size": len(data)}


# p339: Upload with form field after file
@app.post("/p339")
async def handler_p339(file: UploadFile = File(), tag: str = Form()):
    data = await file.read()
    return {"tag": tag, "size": len(data)}


# p340: Upload with default form value
@app.post("/p340")
async def handler_p340(file: UploadFile, label: str = Form(default="default")):
    data = await file.read()
    return {"label": label, "size": len(data)}


# p341: FileResponse serves binary file
@app.get("/p341")
async def handler_p341():
    return FileResponse(_SAMPLE_BIN)


# p342: FileResponse serves HTML file
@app.get("/p342")
async def handler_p342():
    return FileResponse(_SAMPLE_HTML)


# p343: FileResponse with PNG (auto mime)
@app.get("/p343")
async def handler_p343():
    return FileResponse(_SAMPLE_PNG)


# p344: FileResponse with custom status code
@app.get("/p344")
async def handler_p344():
    return FileResponse(_SAMPLE_TXT, status_code=200)


# p345: FileResponse for large file
@app.get("/p345")
async def handler_p345():
    return FileResponse(_LARGE_FILE)


# p346: FileResponse nonexistent file (should 404 or 500)
@app.get("/p346")
async def handler_p346():
    return FileResponse("/nonexistent/file/path.txt")


# p347: Upload two files
@app.post("/p347")
async def handler_p347(file1: UploadFile = File(), file2: UploadFile = File()):
    d1 = await file1.read()
    d2 = await file2.read()
    return {
        "file1_name": file1.filename,
        "file2_name": file2.filename,
        "file1_size": len(d1),
        "file2_size": len(d2),
    }


# p348: Upload file list plus form
@app.post("/p348")
async def handler_p348(title: str = Form(), files: list[UploadFile] = File()):
    names = [f.filename for f in files]
    return {"title": title, "names": names}


# p349: Upload and echo content as plain text response
@app.post("/p349")
async def handler_p349(file: UploadFile):
    data = await file.read()
    return Response(content=data, media_type="application/octet-stream")


# p350: Upload file with specific content type check
@app.post("/p350")
async def handler_p350(file: UploadFile):
    return {"content_type": file.content_type, "filename": file.filename}


# p351-p360: Multipart form patterns

# p351: Form-only (no file)
@app.post("/p351")
async def handler_p351(name: str = Form(), age: int = Form()):
    return {"name": name, "age": age}


# p352: Form with default
@app.post("/p352")
async def handler_p352(name: str = Form(), role: str = Form(default="user")):
    return {"name": name, "role": role}


# p353: Multiple form fields
@app.post("/p353")
async def handler_p353(
    a: str = Form(), b: str = Form(), c: str = Form()
):
    return {"a": a, "b": b, "c": c}


# p354: Form int field
@app.post("/p354")
async def handler_p354(count: int = Form()):
    return {"count": count, "type": "int"}


# p355: Form float field
@app.post("/p355")
async def handler_p355(price: float = Form()):
    return {"price": price}


# p356: Form bool field
@app.post("/p356")
async def handler_p356(active: bool = Form()):
    return {"active": active}


# p357: Form + Query combined
@app.post("/p357")
async def handler_p357(q: str = Query(default=""), name: str = Form()):
    return {"q": q, "name": name}


# p358: Multiple files with count
@app.post("/p358")
async def handler_p358(files: list[UploadFile] = File()):
    total = 0
    for f in files:
        d = await f.read()
        total += len(d)
    return {"count": len(files), "total_size": total}


# p359: File upload returns filename and size
@app.post("/p359")
async def handler_p359(file: UploadFile):
    data = await file.read()
    return {"filename": file.filename, "size": len(data)}


# p360: Form with empty string
@app.post("/p360")
async def handler_p360(value: str = Form()):
    return {"value": value, "empty": value == ""}


# p361-p370: File + other params combined

# p361: File + path param
@app.post("/p361/{category}")
async def handler_p361(category: str, file: UploadFile):
    data = await file.read()
    return {"category": category, "size": len(data)}


# p362: File + query param
@app.post("/p362")
async def handler_p362(tag: str = Query(default="none"), file: UploadFile = File()):
    data = await file.read()
    return {"tag": tag, "size": len(data)}


# p363: File + form + query combined
@app.post("/p363")
async def handler_p363(
    q: str = Query(default=""),
    name: str = Form(),
    file: UploadFile = File(),
):
    data = await file.read()
    return {"q": q, "name": name, "size": len(data)}


# p364: Multiple files + path param
@app.post("/p364/{bucket}")
async def handler_p364(bucket: str, files: list[UploadFile] = File()):
    names = [f.filename for f in files]
    return {"bucket": bucket, "names": names}


# p365: File + form with int
@app.post("/p365")
async def handler_p365(count: int = Form(), file: UploadFile = File()):
    data = await file.read()
    return {"count": count, "size": len(data)}


# p366: Upload returns binary echo
@app.post("/p366")
async def handler_p366(file: UploadFile):
    data = await file.read()
    return Response(content=data, media_type=file.content_type or "application/octet-stream")


# p367: Form with special characters
@app.post("/p367")
async def handler_p367(text: str = Form()):
    return {"text": text}


# p368: File upload to specific endpoint name
@app.post("/p368/upload")
async def handler_p368(file: UploadFile):
    data = await file.read()
    return {"path": "/p368/upload", "size": len(data)}


# p369: FileResponse with headers
@app.get("/p369")
async def handler_p369():
    return FileResponse(
        _SAMPLE_TXT,
        headers={"X-File-Type": "text"},
    )


# p370: Form + File, file is optional (using default=None won't work reliably,
# so test with file present)
@app.post("/p370")
async def handler_p370(name: str = Form(), file: UploadFile = File()):
    data = await file.read()
    return {"name": name, "has_file": True, "size": len(data)}


# ═══════════════════════════════════════════════════════════════════════
# PATTERNS 371-400: Security
# ═══════════════════════════════════════════════════════════════════════

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")
oauth2_scheme_no_error = OAuth2PasswordBearer(tokenUrl="/token", auto_error=False)
http_bearer = HTTPBearer()
http_bearer_no_error = HTTPBearer(auto_error=False)
http_basic = HTTPBasic()
api_key_header = APIKeyHeader(name="X-API-Key")
api_key_header_no_error = APIKeyHeader(name="X-API-Key", auto_error=False)


# p371: OAuth2PasswordBearer returns token
@app.get("/p371")
async def handler_p371(token: str = Depends(oauth2_scheme)):
    return {"token": token}


# p372: OAuth2PasswordBearer missing token -> 401
@app.get("/p372")
async def handler_p372(token: str = Depends(oauth2_scheme)):
    return {"token": token}


# p373: OAuth2PasswordBearer auto_error=False -> returns None
@app.get("/p373")
async def handler_p373(token: str = Depends(oauth2_scheme_no_error)):
    return {"token": token}


# p374: HTTPBearer returns credentials
@app.get("/p374")
async def handler_p374(
    creds: HTTPAuthorizationCredentials = Depends(http_bearer),
):
    return {"scheme": creds.scheme, "credentials": creds.credentials}


# p375: HTTPBearer missing -> 401 or 403
@app.get("/p375")
async def handler_p375(
    creds: HTTPAuthorizationCredentials = Depends(http_bearer),
):
    return {"creds": creds.credentials}


# p376: HTTPBearer wrong scheme -> error
@app.get("/p376")
async def handler_p376(
    creds: HTTPAuthorizationCredentials = Depends(http_bearer),
):
    return {"creds": creds.credentials}


# p377: HTTPBasic returns username/password
@app.get("/p377")
async def handler_p377(
    creds: HTTPBasicCredentials = Depends(http_basic),
):
    return {"username": creds.username, "password": creds.password}


# p378: HTTPBasic missing -> 401 with WWW-Authenticate
@app.get("/p378")
async def handler_p378(
    creds: HTTPBasicCredentials = Depends(http_basic),
):
    return {"username": creds.username}


# p379: APIKeyHeader returns key
@app.get("/p379")
async def handler_p379(api_key: str = Depends(api_key_header)):
    return {"api_key": api_key}


# p380: APIKeyHeader missing -> 403
@app.get("/p380")
async def handler_p380(api_key: str = Depends(api_key_header)):
    return {"api_key": api_key}


# p381: SecurityScopes in dependency
async def get_current_user(
    token: str = Depends(oauth2_scheme),
):
    return {"user": "testuser", "token": token}

@app.get("/p381")
async def handler_p381(user: dict = Depends(get_current_user)):
    return user


# p382: OAuth2PasswordRequestForm fields
@app.post("/p382")
async def handler_p382(form_data: OAuth2PasswordRequestForm = Depends()):
    return {
        "username": form_data.username,
        "password": form_data.password,
        "scopes": form_data.scopes,
        "grant_type": form_data.grant_type,
    }


# p383: Multiple security schemes
async def multi_auth(
    token: str = Depends(oauth2_scheme_no_error),
    api_key: str = Depends(api_key_header_no_error),
):
    return {"token": token, "api_key": api_key}

@app.get("/p383")
async def handler_p383(auth: dict = Depends(multi_auth)):
    return auth


# p384: Security on router level
secure_router = APIRouter(prefix="/secure", dependencies=[Depends(oauth2_scheme)])

@secure_router.get("/p384")
async def handler_p384():
    return {"access": "granted"}

app.include_router(secure_router)


# p385: Security with dependency chain
async def get_token(token: str = Depends(oauth2_scheme)):
    return token

async def get_user_from_token(token: str = Depends(get_token)):
    return {"user": "alice", "token": token}

@app.get("/p385")
async def handler_p385(user: dict = Depends(get_user_from_token)):
    return user


# p386: OAuth2 bearer with valid token through dependency
async def verify_token(token: str = Depends(oauth2_scheme)):
    if token == "valid-token":
        return {"verified": True}
    return {"verified": False}

@app.get("/p386")
async def handler_p386(result: dict = Depends(verify_token)):
    return result


# p387: HTTPBearer auto_error=False -> None
@app.get("/p387")
async def handler_p387(
    creds: HTTPAuthorizationCredentials = Depends(http_bearer_no_error),
):
    if creds is None:
        return {"creds": None}
    return {"creds": creds.credentials}


# p388: APIKey with auto_error=False
@app.get("/p388")
async def handler_p388(api_key: str = Depends(api_key_header_no_error)):
    return {"api_key": api_key}


# p389: Security dependency returns custom response
async def check_admin(token: str = Depends(oauth2_scheme)):
    if token != "admin-token":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin only")
    return token

@app.get("/p389")
async def handler_p389(token: str = Depends(check_admin)):
    return {"admin": True, "token": token}


# p390: Basic auth with correct credentials
@app.get("/p390")
async def handler_p390(creds: HTTPBasicCredentials = Depends(http_basic)):
    if creds.username == "admin" and creds.password == "secret":
        return {"authenticated": True}
    from fastapi import HTTPException
    raise HTTPException(status_code=401, detail="Bad credentials")


# p391: OAuth2 token with scopes in form
@app.post("/p391")
async def handler_p391(form_data: OAuth2PasswordRequestForm = Depends()):
    return {
        "username": form_data.username,
        "scopes": form_data.scopes,
    }


# p392: Dependency chain three levels deep
async def dep_level1(token: str = Depends(oauth2_scheme)):
    return {"level": 1, "token": token}

async def dep_level2(l1: dict = Depends(dep_level1)):
    return {**l1, "level": 2}

async def dep_level3(l2: dict = Depends(dep_level2)):
    return {**l2, "level": 3}

@app.get("/p392")
async def handler_p392(data: dict = Depends(dep_level3)):
    return data


# p393: OAuth2 with Bearer prefix check
@app.get("/p393")
async def handler_p393(token: str = Depends(oauth2_scheme)):
    return {"token_length": len(token)}


# p394: HTTPBasic with unicode password
@app.get("/p394")
async def handler_p394(creds: HTTPBasicCredentials = Depends(http_basic)):
    return {"username": creds.username, "password": creds.password}


# p395: APIKeyHeader custom name
api_key_custom = APIKeyHeader(name="X-Custom-Key")

@app.get("/p395")
async def handler_p395(key: str = Depends(api_key_custom)):
    return {"key": key}


# p396: Security returns 401 status code on missing Bearer
@app.get("/p396")
async def handler_p396(token: str = Depends(oauth2_scheme)):
    return {"token": token}


# p397: Bearer token content preserved exactly
@app.get("/p397")
async def handler_p397(token: str = Depends(oauth2_scheme)):
    return {"token": token, "length": len(token)}


# p398: HTTPBasic with empty password
@app.get("/p398")
async def handler_p398(creds: HTTPBasicCredentials = Depends(http_basic)):
    return {"username": creds.username, "password": creds.password}


# p399: Multi-level dependency with security
async def get_db():
    return {"db": "connected"}

async def get_user_with_db(
    token: str = Depends(oauth2_scheme),
    db: dict = Depends(get_db),
):
    return {"token": token, **db}

@app.get("/p399")
async def handler_p399(ctx: dict = Depends(get_user_with_db)):
    return ctx


# p400: Security dependency order preserved
async def dep_a(token: str = Depends(oauth2_scheme)):
    return f"a:{token}"

async def dep_b(a: str = Depends(dep_a)):
    return f"b:{a}"

@app.get("/p400")
async def handler_p400(result: str = Depends(dep_b)):
    return {"result": result}


# ═══════════════════════════════════════════════════════════════════════
# Health check
# ═══════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"ok": True}
