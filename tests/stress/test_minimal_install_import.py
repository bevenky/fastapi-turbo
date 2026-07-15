"""Minimal-install import safety: ``import fastapi_turbo`` must work
WITHOUT starlette's optional extras (jinja2, itsdangerous).

The release wheel smoke caught the compat shim eagerly importing
``fastapi.templating`` / ``starlette.templating`` (need jinja2) and
``starlette.middleware.sessions`` (needs itsdangerous) at install time —
so a clean ``pip install fastapi-turbo`` crashed on import while stock
FastAPI defers those errors until the user imports the module. Dev/CI
environments never reproduce it because the extras are always installed.

Simulated here by poisoning ``sys.modules`` (``None`` entry => ImportError
on import) in a subprocess, which is exactly the sequencing a clean
minimal env produces.
"""

import subprocess
import sys

_BLOCK = "import sys; sys.modules['jinja2'] = None; sys.modules['itsdangerous'] = None\n"


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _BLOCK + code],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_import_and_serve_without_optional_extras():
    proc = _run(
        "import fastapi_turbo\n"
        "from fastapi import FastAPI\n"
        "from fastapi.testclient import TestClient\n"
        "app = FastAPI()\n"
        "@app.get('/ping')\n"
        "def ping():\n"
        "    return {'ok': True}\n"
        "with TestClient(app, in_process=True) as c:\n"
        "    r = c.get('/ping')\n"
        "assert r.status_code == 200, r.status_code\n"
        "print('MINIMAL-OK')\n"
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "MINIMAL-OK" in proc.stdout


def test_templating_import_keeps_stock_lazy_error():
    """Directly importing templating without jinja2 raises the same
    ImportError stock raises — not something turbo-specific/earlier."""
    proc = _run(
        "import fastapi_turbo\n"
        "try:\n"
        "    import fastapi.templating\n"
        "except ImportError as e:\n"
        "    print('LAZY-ERR:', e)\n"
        "else:\n"
        "    print('NO-ERROR')\n"
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "LAZY-ERR:" in proc.stdout, proc.stdout
    assert "jinja2" in proc.stdout


def test_sessions_import_keeps_stock_lazy_error():
    proc = _run(
        "import fastapi_turbo\n"
        "try:\n"
        "    import starlette.middleware.sessions\n"
        "except ImportError as e:\n"
        "    print('LAZY-ERR:', e)\n"
        "else:\n"
        "    print('NO-ERROR')\n"
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "LAZY-ERR:" in proc.stdout, proc.stdout
