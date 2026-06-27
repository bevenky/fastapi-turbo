"""Combined HTML report: conn=1 per-request latency (core-independent) +
FAIR all-core throughput (every framework on all 18 cores). Reads
results_matrix.json (latency) and results_throughput.json (throughput).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAT = json.loads((HERE / "results_matrix.json").read_text())
TP = json.loads((HERE / "results_throughput.json").read_text())
TPMETA, TPDATA = TP["meta"], TP["data"]
OUT = HERE / "report.html"

FW = ["fastapi-turbo", "FastAPI (uvicorn)", "Gin (Go)", "Fastify (Node)", "raw-axum"]
SHORT = {"fastapi-turbo": "turbo", "FastAPI (uvicorn)": "FastAPI", "Gin (Go)": "Gin",
         "Fastify (Node)": "Fastify", "raw-axum": "raw-Axum"}

# rows in display order: (label_latency, label_throughput, group)
ROWS = [
    ("ping", None, "Baseline"),
    ("GET hello (sync)", "GET hello sync", "Simple JSON"),
    ("GET hello (async)", "GET hello async", "Simple JSON"),
    ("GET large JSON", "GET large JSON", "Large JSON (1000 items)"),
    ("GET large JSON async", "GET large JSON async", "Large JSON (1000 items)"),
    ("POST items (sync)", "POST items sync", "POST / PUT / PATCH / DELETE"),
    ("POST items (async)", "POST items async", "POST / PUT / PATCH / DELETE"),
    ("PUT items/{id}", "PUT items", "POST / PUT / PATCH / DELETE"),
    ("PATCH items/{id}", "PATCH items", "POST / PUT / PATCH / DELETE"),
    ("DELETE items/{id}", "DELETE items", "POST / PUT / PATCH / DELETE"),
    ("GET XML small", "XML small", "XML"),
    ("GET XML large", "XML large", "XML"),
    ("stream sync", "stream sync", "Streaming"),
    ("stream async", "stream async", "Streaming"),
    ("stream await", "stream await", "Streaming"),
    ("redis GET sync", "redis GET sync", "Redis"),
    ("redis GET async", "redis GET async", "Redis"),
    ("redis SET sync", "redis SET sync", "Redis"),
    ("redis SET async", "redis SET async", "Redis"),
    ("pg item sync", "pg item sync", "Postgres"),
    ("pg item async", "pg item async", "Postgres"),
    ("pg list sync", "pg list sync", "Postgres"),
    ("pg list async", "pg list async", "Postgres"),
]


def lat(fw, label):
    return (LAT.get(fw, {}).get(label) or {}).get("p50_us") if label else None


def tp(fw, label):
    return (TPDATA.get(fw, {}).get(label) or {}).get("rps") if label else None


def cores(fw, label):
    return (TPDATA.get(fw, {}).get(label) or {}).get("cores") if label else None


def fnum(v):
    if v is None:
        return '<span class=na>—</span>'
    return f"{v:,.0f}" if isinstance(v, float) else f"{v:,}"


def cell(fw, v, best, better):
    cls = "turbo " if fw == "fastapi-turbo" else ""
    if v is not None and best is not None and v == best:
        cls += "best"
    return f'<td class="{cls.strip()}">{fnum(v)}</td>'


def table(metric, getter, suffix, better, extra=None):
    h = [f'<table><tr><th class=lab>endpoint</th>']
    for fw in FW:
        h.append(f'<th class="{"turbo" if fw=="fastapi-turbo" else ""}">{SHORT[fw]}</th>')
    h.append('</tr>')
    cur = None
    for lat_lbl, tp_lbl, grp in ROWS:
        lbl = lat_lbl if metric == "lat" else tp_lbl
        if lbl is None and metric != "lat":
            continue
        if grp != cur:
            cur = grp
            h.append(f'<tr class=grp><td class=lab colspan={len(FW)+1}>{grp}</td></tr>')
        vals = {fw: getter(fw, lat_lbl if metric == "lat" else tp_lbl) for fw in FW}
        present = [v for v in vals.values() if v is not None]
        best = (min if better == "low" else max)(present) if present else None
        rowlbl = lat_lbl
        h.append(f'<tr><td class=lab>{rowlbl}</td>')
        for fw in FW:
            v = vals[fw]
            sub = ""
            if extra:
                c = extra(fw, tp_lbl)
                if c is not None:
                    sub = f' <span class=metric>{c}c</span>'
            cls = "turbo " if fw == "fastapi-turbo" else ""
            if v is not None and best is not None and v == best:
                cls += "best"
            h.append(f'<td class="{cls.strip()}">{fnum(v)}{sub}</td>')
        h.append('</tr>')
    h.append('</table>')
    return "".join(h)


html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>fastapi-turbo — fair cross-framework benchmark</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0d1117;color:#e6edf3}}
 .wrap{{max-width:1400px;margin:0 auto;padding:28px}}
 h1{{font-size:24px;margin:0 0 6px}} h2{{font-size:18px;margin:34px 0 4px;color:#58a6ff}}
 .sub{{color:#8b949e;margin:0 0 10px;font-size:13px}}
 table{{border-collapse:collapse;width:100%;margin:8px 0 4px;font-variant-numeric:tabular-nums}}
 th,td{{padding:5px 10px;text-align:right;border-bottom:1px solid #21262d}}
 th{{color:#8b949e;background:#161b22}}
 td.lab,th.lab{{text-align:left;white-space:nowrap}}
 tr.grp td{{background:#161b22;color:#58a6ff;font-size:11px;text-transform:uppercase;letter-spacing:.5px;font-weight:700}}
 .turbo{{background:#11261b}} th.turbo{{color:#3fb950}}
 .best{{color:#3fb950;font-weight:700}}
 .na{{color:#484f58}} .metric{{font-size:10px;color:#6e7681}}
 code{{color:#79c0ff}} .key{{color:#d29922}}
</style></head><body><div class=wrap>
<h1>fastapi-turbo — fair cross-framework benchmark</h1>
<p class=sub>{datetime.now().strftime('%Y-%m-%d %H:%M')} · Apple M5 Max, {TPMETA['cores']} cores · loopback, solo-boot.
<b>Latency</b> = conn=1 p50 µs (one request at a time; core-count independent).
<b>Throughput</b> = ALL frameworks on all {TPMETA['cores']} cores
(turbo/uvicorn workers={TPMETA['workers']}, Fastify cluster={TPMETA['workers']}, Gin/Axum native), oha c={TPMETA['conc']}.
<span class=metric>NNc = cores used.</span> <span class=best>Green</span> = best in row.</p>

<h2>① Per-request latency — conn=1 p50 (µs, lower better)</h2>
<p class=sub>How fast is a single request. Independent of core count — this is per-core efficiency.</p>
{table("lat", lat, "µs", "low")}

<h2>② Throughput — all {TPMETA['cores']} cores, c={TPMETA['conc']} (req/s, higher better)</h2>
<p class=sub>Max requests/sec with every framework using all cores (the real deployment number). Small grey number = cores actually used.</p>
{table("tp", tp, "", "high", extra=cores)}
</div></body></html>"""

OUT.write_text(html)
print(f"wrote {OUT}")
