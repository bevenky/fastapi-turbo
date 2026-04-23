"""File handling benchmark server — fastapi-turbo.

Endpoints:
  POST /upload          — multipart/form-data, single file
  GET  /download/:name  — FileResponse
  GET  /static/:name    — StaticFiles mount
  GET  /health
"""

import os
import sys
import tempfile

from fastapi_turbo import FastAPI, UploadFile
from fastapi_turbo.middleware.gzip import GZipMiddleware
from fastapi_turbo.responses import FileResponse, JSONResponse
from fastapi_turbo.staticfiles import StaticFiles


# Create a temp directory with test files for download/static
TMP_DIR = tempfile.mkdtemp(prefix="bench_files_")
for name, content in [
    ("small.txt", b"hello world\n" * 10),          # ~120 bytes
    ("medium.bin", b"x" * (64 * 1024)),            # 64 KB
    ("large.bin", b"x" * (1024 * 1024)),           # 1 MB
    ("style.css", b"body{color:red;}" * 100),      # ~1.5 KB
]:
    with open(os.path.join(TMP_DIR, name), "wb") as f:
        f.write(content)


app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=500)


# Build a payload that's compressible (repeated keys) so gzip is meaningful
_COMPRESSIBLE = [{"id": i, "name": f"item-{i}", "desc": "lorem ipsum dolor sit amet " * 4}
                 for i in range(200)]


@app.get("/health")
async def health():
    return JSONResponse({"ok": True})


@app.get("/json-big")
async def json_big():
    # ~60 KB JSON payload — compresses very well
    return JSONResponse(_COMPRESSIBLE)


@app.post("/upload")
async def upload(file: UploadFile):
    data = await file.read()
    return JSONResponse({"filename": file.filename, "size": len(data)})


@app.get("/download/{name}")
def download(name: str):
    return FileResponse(os.path.join(TMP_DIR, name))


app.mount("/static", StaticFiles(directory=TMP_DIR), name="static")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8500"))
    app.run("127.0.0.1", port)
