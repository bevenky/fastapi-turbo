"""Render results_db.json → an HTML section: cross-framework PG/Redis (all 5)
+ Python driver matrix (turbo + uvicorn). Appends to report.html as report_db.html."""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = json.loads((HERE / "results_db.json").read_text())
META, DATA = DB["meta"], DB["data"]
OUT = HERE / "report_db.html"

FW = ["fastapi-turbo", "FastAPI (uvicorn)", "Gin (Go)", "Fastify (Node)", "raw-axum"]
SHORT = {"fastapi-turbo": "turbo", "FastAPI (uvicorn)": "FastAPI", "Gin (Go)": "Gin",
         "Fastify (Node)": "Fastify", "raw-axum": "raw-Axum"}
PYFW = ["fastapi-turbo", "FastAPI (uvicorn)"]


def rps(fw, label):
    return (DATA.get(fw, {}).get(label) or {}).get("rps")


def cores(fw, label):
    return (DATA.get(fw, {}).get(label) or {}).get("cores")


def fnum(v):
    return '<span class=na>—</span>' if v is None else f"{v:,.0f}"


def table(cols, labels):
    h = ['<table><tr><th class=lab>endpoint</th>']
    for fw in cols:
        h.append(f'<th class="{"turbo" if fw=="fastapi-turbo" else ""}">{SHORT[fw]}</th>')
    h.append('</tr>')
    for label in labels:
        vals = {fw: rps(fw, label) for fw in cols}
        present = [v for v in vals.values() if v is not None]
        best = max(present) if present else None
        h.append(f'<tr><td class=lab>{label}</td>')
        for fw in cols:
            v = vals[fw]; c = cores(fw, label)
            cls = "turbo " if fw == "fastapi-turbo" else ""
            if v is not None and v == best:
                cls += "best"
            sub = f' <span class=metric>{c}c</span>' if c is not None else ""
            h.append(f'<td class="{cls.strip()}">{fnum(v)}{sub}</td>')
        h.append('</tr>')
    h.append('</table>')
    return "".join(h)


# label groups
io = ("sync", "async")
cross_reads = [f"PG select_one [{x}]" for x in io] + [f"PG select_list [{x}]" for x in io]
cross_writes = []
for op in ("insert", "update", "delete"):
    for c in ("true", "false"):
        for x in io:
            cross_writes.append(f"PG {op} commit={c} [{x}]")
cross_redis = [f"redis {r} [{m}]" for r in ("get", "set", "set_durable", "pipeline", "multi") for m in io]
pym = []
for drv in ("pg3sync", "pg2sync", "pg3async", "asyncpg"):
    pym += [f"{drv} select_one", f"{drv} select_list"]
    for op in ("insert", "update", "delete"):
        for c in ("true", "false"):
            pym.append(f"{drv} {op} commit={c}")

html = f"""<!doctype html><html><head><meta charset=utf-8><title>DB/Redis matrix</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0d1117;color:#e6edf3}}
 .wrap{{max-width:1300px;margin:0 auto;padding:28px}} h1{{font-size:23px}} h2{{font-size:17px;color:#58a6ff;margin-top:30px}}
 .sub{{color:#8b949e;font-size:13px}} table{{border-collapse:collapse;width:100%;margin:8px 0;font-variant-numeric:tabular-nums}}
 th,td{{padding:5px 10px;text-align:right;border-bottom:1px solid #21262d}} th{{color:#8b949e;background:#161b22}}
 td.lab,th.lab{{text-align:left;white-space:nowrap}} .turbo{{background:#11261b}} th.turbo{{color:#3fb950}}
 .best{{color:#3fb950;font-weight:700}} .na{{color:#484f58}} .metric{{font-size:10px;color:#6e7681}} code{{color:#79c0ff}}
</style></head><body><div class=wrap>
<h1>Postgres + Redis deep matrix</h1>
<p class=sub>{datetime.now().strftime('%Y-%m-%d %H:%M')} · {META['cores']} cores · all frameworks workers/cluster={META['db_workers']}
(Postgres 100-conn budget) · oha c={META['conc']}, {META['dur']} · req/s, higher better. <span class=best>green</span>=best in row.
turbo/FastAPI = multiprocess (one pool PER worker); Gin/Fastify/Axum = one shared pool. set_durable = AOF appendfsync=always.</p>

<h2>Cross-framework — Postgres reads (req/s)</h2>
{table(FW, cross_reads)}
<h2>Cross-framework — Postgres writes: commit=true (durable) vs false (rollback)</h2>
{table(FW, cross_writes)}
<h2>Cross-framework — Redis (req/s)</h2>
{table(FW, cross_redis)}
<h2>Python Postgres driver matrix (turbo vs uvicorn) — req/s</h2>
<p class=sub>psycopg3-sync, psycopg2-sync, psycopg3-async, asyncpg. Exposes which driver wins and whether the async catastrophe is asyncpg-specific.</p>
{table(PYFW, pym)}
</div></body></html>"""
OUT.write_text(html)
print(f"wrote {OUT}")
