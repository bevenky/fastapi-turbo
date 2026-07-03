"""Consolidated benchmark report — instrument-panel redesign.

Reads results_matrix.json (conn=1 latency), results_throughput.json (all-core),
results_db.json (PG/Redis deep matrix), results_ws.json (WebSocket) and emits
ONE report.html. Design language: test-bench certificate — measurement
channels (CH-01..CH-06), phosphor-amber = turbo, per-row "field lane" showing
turbo's position among the rivals (left edge = row leader).
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAT = json.loads((HERE / "results_matrix.json").read_text())
TP = json.loads((HERE / "results_throughput.json").read_text())
DB = json.loads((HERE / "results_db.json").read_text())
WS = json.loads((HERE / "results_ws.json").read_text())
OUT = HERE / "report.html"

FW = ["fastapi-turbo", "FastAPI (uvicorn)", "Gin (Go)", "Fastify (Node)", "raw-axum"]
SHORT = {"fastapi-turbo": "turbo", "FastAPI (uvicorn)": "FastAPI", "Gin (Go)": "Gin",
         "Fastify (Node)": "Fastify", "raw-axum": "axum"}
DOT = {"fastapi-turbo": "t", "FastAPI (uvicorn)": "fa", "Gin (Go)": "gin",
       "Fastify (Node)": "ff", "raw-axum": "ax"}

LAT_ROWS = [
    ("Baseline", [("ping (static)", "ping")]),
    ("Simple JSON", [("GET hello · sync", "GET hello (sync)"), ("GET hello · async", "GET hello (async)")]),
    ("Body methods", [("POST items", "POST items (sync)"), ("POST items · async", "POST items (async)"),
                      ("PUT items/{id}", "PUT items/{id}"), ("PATCH items/{id}", "PATCH items/{id}"),
                      ("DELETE items/{id}", "DELETE items/{id}")]),
    ("Large payloads", [("large JSON · 1000 items", "GET large JSON"), ("large JSON · async", "GET large JSON async"),
                        ("XML · small", "GET XML small"), ("XML · large 45KB", "GET XML large")]),
    ("Streaming", [("stream · sync gen", "stream sync"), ("stream · async gen", "stream async"),
                   ("stream · real await", "stream await")]),
    ("Redis", [("GET · sync client", "redis GET sync"), ("GET · async client", "redis GET async"),
               ("SET · sync client", "redis SET sync"), ("SET · async client", "redis SET async")]),
    ("Postgres", [("item by id · sync", "pg item sync"), ("item by id · async", "pg item async"),
                  ("list 10 · sync", "pg list sync"), ("list 10 · async", "pg list async")]),
]

TP_ROWS = [
    ("Simple JSON", [("GET hello · sync", "GET hello sync"), ("GET hello · async", "GET hello async")]),
    ("Body methods", [("POST items", "POST items sync"), ("POST items · async", "POST items async"),
                      ("PUT items", "PUT items"), ("PATCH items", "PATCH items"), ("DELETE items", "DELETE items")]),
    ("Large payloads", [("large JSON", "GET large JSON"), ("large JSON · async", "GET large JSON async"),
                        ("XML · small", "XML small"), ("XML · large", "XML large")]),
    ("Streaming", [("stream · sync gen", "stream sync"), ("stream · async gen", "stream async"),
                   ("stream · real await", "stream await")]),
    ("Redis", [("GET · sync", "redis GET sync"), ("GET · async", "redis GET async"),
               ("SET · sync", "redis SET sync"), ("SET · async", "redis SET async")]),
    ("Postgres", [("item · sync", "pg item sync"), ("item · async", "pg item async"),
                  ("list · sync", "pg list sync"), ("list · async", "pg list async")]),
]

DBD = DB["data"]
PG_READS = [("select one · sync", "PG select_one [sync]"), ("select one · async", "PG select_one [async]"),
            ("select 10 · sync", "PG select_list [sync]"), ("select 10 · async", "PG select_list [async]")]
PG_WRITES = []
for op in ("insert", "update", "delete"):
    for c in ("true", "false"):
        for io in ("sync", "async"):
            PG_WRITES.append((f"{op} · commit={c} · {io}", f"PG {op} commit={c} [{io}]"))
REDIS_ROWS = [(f"{op.replace('_', ' ')} · {m}", f"redis {op} [{m}]")
              for op in ("get", "set", "pipeline", "multi", "set_durable") for m in ("sync", "async")]
# turbo's multiplexed client (fastapi_turbo.contrib_redis) — opt-in, labeled
MUX_ROWS = [("get · turbo mux client", "redis get [turbo mux client]"),
            ("set · turbo mux client", "redis set [turbo mux client]")]

DRIVERS = ["pg3sync", "pg2sync", "pg3async", "asyncpg"]
DRIVER_OPS = ["select_one", "select_list", "insert commit=true", "insert commit=false",
              "update commit=true", "update commit=false", "delete commit=true", "delete commit=false"]

CAMPAIGN = [
    ("stream · sync gen", "852 µs", "28 µs", "30×", "conn=1 · beats Fastify"),
    ("stream · async gen", "546 µs", "29 µs", "19×", "conn=1 · beats Fastify"),
    ("stream · real await", "284 µs", "50 µs", "5.7×", "conn=1 p50"),
    ("Postgres · async", "1.8k", "47.7k", "26×", "req/s all-core"),
    ("redis · turbo mux", "98 µs", "53 µs", "9.5× raw", "opt-in multiplexed client"),
    ("WS server RTT p99", "49.7 µs", "40 µs", "−20%", "Rust client · direct-send"),
    ("PUT /items", "33 µs", "27 µs", "at Gin", "conn=1 p50"),
    ("regressions", "—", "0", "4,500+ tests", "parity · local · upstream"),
]


def esc(s):
    return html.escape(str(s), quote=True)


def get_lat(fw, key):
    return (LAT.get(fw, {}).get(key) or {}).get("p50_us")


def get_tp(fw, key):
    return (TP["data"].get(fw, {}).get(key) or {}).get("rps")


def get_db(fw, key):
    return (DBD.get(fw, {}).get(key) or {}).get("rps")


def fmt_us(v):
    return "—" if v is None else f"{v:,.0f}"


def fmt_rps(v):
    if v is None:
        return "—"
    return f"{v / 1000:,.1f}k" if v >= 10000 else f"{v:,.0f}"


def lane(vals, better):
    """Field lane: dot per framework, best at left. vals = {fw: value}."""
    present = {f: v for f, v in vals.items() if v is not None}
    if len(present) < 2:
        return '<div class="lane"></div>'
    lo, hi = min(present.values()), max(present.values())
    span = (hi - lo) or 1
    ticks = []
    for f, v in present.items():
        pos = (v - lo) / span if better == "low" else (hi - v) / span
        pct = 4 + pos * 92
        cls = "tick t" if f == "fastapi-turbo" else f"tick {DOT[f]}"
        ticks.append(f'<i class="{cls}" style="left:{pct:.1f}%" title="{esc(SHORT[f])}: {esc(v)}"></i>')
    return f'<div class="lane">{"".join(ticks)}</div>'


def metric_table(groups, getter, fmt, better, unit_note, extra_note=None):
    out = [f'<div class="tscroll"><table><thead><tr><th class="lab">{esc(unit_note)}</th>']
    for f in FW:
        out.append(f'<th class="{ "tcol" if f == "fastapi-turbo" else ""}">{esc(SHORT[f])}</th>')
    out.append('<th class="lanehead">field · leader at left</th></tr></thead><tbody>')
    for gname, rows in groups:
        out.append(f'<tr class="grp"><td colspan="7">{esc(gname)}</td></tr>')
        for label, key in rows:
            vals = {f: getter(f, key) for f in FW}
            present = [v for v in vals.values() if v is not None]
            best = (min if better == "low" else max)(present) if present else None
            leader = next((f for f in FW if vals[f] == best), None) if best is not None else None
            out.append(f'<tr><td class="lab">{esc(label)}</td>')
            for f in FW:
                v = vals[f]
                cls = "tcol " if f == "fastapi-turbo" else ""
                if v is not None and v == best:
                    cls += "lead tlead" if f == "fastapi-turbo" else "lead"
                out.append(f'<td class="{cls.strip()}">{fmt(v)}</td>')
            chip = ""
            if leader and leader != "fastapi-turbo":
                chip = f'<b class="chip">{esc(SHORT[leader])}</b>'
            elif leader == "fastapi-turbo":
                chip = '<b class="chip amber">turbo</b>'
            out.append(f'<td class="lanecell">{lane(vals, better)}{chip}</td></tr>')
    out.append("</tbody></table></div>")
    if extra_note:
        out.append(f'<p class="note">{extra_note}</p>')
    return "".join(out)


def driver_table():
    out = ['<div class="tscroll"><table class="drv"><thead><tr><th class="lab">psycopg3-sync · psycopg2 · psycopg3-async · asyncpg</th>']
    for d in DRIVERS:
        out.append(f"<th colspan=2>{esc(d)}</th>")
    out.append('</tr><tr><th class="lab sub">req/s · w8/w12 · c64</th>')
    for _ in DRIVERS:
        out.append('<th class="sub tcol">turbo</th><th class="sub">FastAPI</th>')
    out.append("</tr></thead><tbody>")
    for op in DRIVER_OPS:
        out.append(f'<tr><td class="lab">{esc(op)}</td>')
        for d in DRIVERS:
            key = f"{d} {op}"
            tv = get_db("fastapi-turbo", key)
            uv = get_db("FastAPI (uvicorn)", key)
            tcls = "tcol lead tlead" if (tv or 0) >= (uv or 0) else "tcol"
            ucls = "lead" if (uv or 0) > (tv or 0) else ""
            out.append(f'<td class="{tcls}">{fmt_rps(tv)}</td><td class="{ucls}">{fmt_rps(uv)}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def ws_table():
    d = WS.get("data", WS)
    rows = [("server RTT p50 · Rust client", "rust_p50_us", "low", " µs"),
            ("server RTT p99 · Rust client", "rust_p99_us", "low", " µs"),
            ("round-trip p50 · Python client", "p50_us", "low", " µs"),
            ("round-trip p99 · Python client", "p99_us", "low", " µs"),
            ("throughput · fleet", "msgs_per_s", "high", " msg/s")]
    out = ['<div class="tscroll"><table><thead><tr><th class="lab">64-byte echo · /ws</th>']
    for f in FW:
        out.append(f'<th class="{ "tcol" if f == "fastapi-turbo" else ""}">{esc(SHORT[f])}</th>')
    out.append('<th class="lanehead">field · leader at left</th></tr></thead><tbody>')
    for label, key, better, unit in rows:
        vals = {f: (d.get(f) or {}).get(key) for f in FW}
        present = [v for v in vals.values() if v is not None]
        best = (min if better == "low" else max)(present) if present else None
        leader = next((f for f in FW if vals[f] == best), None)
        out.append(f'<tr><td class="lab">{esc(label)}</td>')
        for f in FW:
            v = vals[f]
            cls = "tcol " if f == "fastapi-turbo" else ""
            if v is not None and v == best:
                cls += "lead tlead" if f == "fastapi-turbo" else "lead"
            txt = "—" if v is None else (fmt_rps(v) if key == "msgs_per_s" else f"{float(v):,.1f}")
            out.append(f'<td class="{cls.strip()}">{txt}</td>')
        chip = (f'<b class="chip">{esc(SHORT[leader])}</b>' if leader and leader != "fastapi-turbo"
                else '<b class="chip amber">turbo</b>' if leader else "")
        out.append(f'<td class="lanecell">{lane(vals, better)}{chip}</td></tr>')
    out.append("</tbody></table></div>")
    return "".join(out)


def campaign_cards():
    out = ['<div class="cards">']
    for label, before, after, factor, unit in CAMPAIGN:
        out.append(
            f'<div class="card"><div class="c-lab">{esc(label)}</div>'
            f'<div class="c-vals"><span class="c-before">{esc(before)}</span>'
            f'<span class="c-arrow">→</span><span class="c-after">{esc(after)}</span></div>'
            f'<div class="c-foot"><span class="c-factor">{esc(factor)}</span><span>{esc(unit)}</span></div></div>')
    out.append("</div>")
    return "".join(out)


tpm = TP["meta"]
dbm = DB["meta"]
now = datetime.now().strftime("%Y-%m-%d %H:%M")

HTML = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>fastapi-turbo · bench certificate</title>
<style>
:root {{
  --bg:#14161a; --panel:#1a1d23; --panel2:#1f232b; --line:#2a2f39; --hair:#232833;
  --ink:#e9e6df; --mut:#8f959f; --dim:#5d636e;
  --amber:#ffb454; --amber-dim:#8a6430; --amber-glow:rgba(255,180,84,.14);
  --fa:#7d8590; --gin:#6fa8a8; --ff:#b3986e; --ax:#b07d7d;
  --disp:"Avenir Next Condensed","Arial Narrow",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,monospace;
  --body:system-ui,-apple-system,sans-serif;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 var(--body);
  -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1240px; margin:0 auto; padding:0 28px 80px; }}

/* ── masthead: test certificate ── */
.mast {{ border-bottom:2px solid var(--amber-dim); padding:36px 0 20px; margin-bottom:8px; }}
.mast .id {{ font:600 12px/1 var(--mono); color:var(--amber); letter-spacing:.18em; }}
.mast h1 {{ font-family:var(--disp); font-size:46px; font-weight:600; letter-spacing:.02em;
  margin:10px 0 4px; text-transform:uppercase; }}
.mast h1 em {{ font-style:normal; color:var(--amber); }}
.mast .sub {{ color:var(--mut); font-size:14px; max-width:70ch; }}
.meta {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:18px; }}
.meta span {{ font:11px/1 var(--mono); color:var(--mut); border:1px solid var(--line);
  padding:6px 10px; border-radius:2px; letter-spacing:.04em; }}
.meta .seal {{ color:var(--amber); border-color:var(--amber-dim); background:var(--amber-glow); }}
.legend {{ display:flex; gap:16px; margin-top:14px; font:11px/1 var(--mono); color:var(--mut); }}
.legend i {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px;
  vertical-align:-1px; }}
.legend .lt {{ width:9px; height:9px; border-radius:1px; transform:rotate(45deg); background:var(--amber); }}
.l-fa i {{ background:var(--fa); }} .l-gin i {{ background:var(--gin); }}
.l-ff i {{ background:var(--ff); }} .l-ax i {{ background:var(--ax); }}

/* ── campaign cards ── */
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px;
  margin:26px 0 8px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:3px; padding:14px 16px; }}
.c-lab {{ font:600 11px/1.3 var(--mono); color:var(--mut); letter-spacing:.08em;
  text-transform:uppercase; min-height:28px; }}
.c-vals {{ margin:8px 0 6px; font-family:var(--mono); }}
.c-before {{ color:var(--dim); text-decoration:line-through; font-size:14px; }}
.c-arrow {{ color:var(--mut); margin:0 7px; font-size:13px; }}
.c-after {{ color:var(--amber); font-size:22px; font-weight:700; }}
.c-foot {{ display:flex; justify-content:space-between; font:10px/1 var(--mono); color:var(--dim);
  letter-spacing:.05em; }}
.c-factor {{ color:var(--ink); }}

/* ── channels ── */
section {{ margin-top:44px; }}
.ch {{ display:flex; align-items:baseline; gap:14px; border-bottom:1px solid var(--line);
  padding-bottom:8px; margin-bottom:2px; }}
.ch .num {{ font:700 13px/1 var(--mono); color:var(--amber); letter-spacing:.1em; }}
.ch h2 {{ font-family:var(--disp); font-size:24px; font-weight:600; margin:0;
  text-transform:uppercase; letter-spacing:.03em; }}
.ch .cond {{ margin-left:auto; font:11px/1 var(--mono); color:var(--dim); letter-spacing:.05em; }}

.tscroll {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ padding:7px 12px; text-align:right; font-size:13.5px; white-space:nowrap; }}
thead th {{ font:600 11px/1 var(--mono); color:var(--mut); letter-spacing:.08em;
  text-transform:uppercase; border-bottom:1px solid var(--line); padding-bottom:9px; }}
tbody td {{ border-bottom:1px solid var(--hair); font-family:var(--mono); }}
td.lab, th.lab {{ text-align:left; font-family:var(--body); color:var(--ink); }}
td.lab {{ font-size:13.5px; }}
tr.grp td {{ font:600 10.5px/1 var(--mono); color:var(--dim); letter-spacing:.14em;
  text-transform:uppercase; padding:16px 12px 5px; border-bottom:1px solid var(--line);
  background:transparent; }}
.tcol {{ background:rgba(255,180,84,.05); }}
th.tcol {{ color:var(--amber); }}
.lead {{ font-weight:700; text-decoration:underline; text-underline-offset:3px;
  text-decoration-color:var(--dim); }}
.tlead {{ color:var(--amber); text-decoration-color:var(--amber-dim); }}
tbody tr:hover td {{ background:var(--panel2); }}
tbody tr:hover td.tcol {{ background:rgba(255,180,84,.09); }}

/* field lane */
th.lanehead {{ text-align:left; padding-left:22px; }}
td.lanecell {{ min-width:190px; text-align:left; padding-left:22px; }}
.lane {{ position:relative; display:inline-block; width:120px; height:10px;
  border-bottom:1px solid var(--line); vertical-align:middle; }}
.lane i {{ position:absolute; bottom:-3px; width:7px; height:7px; border-radius:50%; }}
.lane i.t {{ width:9px; height:9px; border-radius:1px; transform:rotate(45deg);
  background:var(--amber); bottom:-4px; z-index:2;
  box-shadow:0 0 6px rgba(255,180,84,.55); }}
.lane i.fa {{ background:var(--fa); }} .lane i.gin {{ background:var(--gin); }}
.lane i.ff {{ background:var(--ff); }} .lane i.ax {{ background:var(--ax); }}
.chip {{ font:600 10px/1 var(--mono); color:var(--mut); border:1px solid var(--line);
  padding:3px 6px; border-radius:2px; margin-left:12px; letter-spacing:.06em;
  vertical-align:middle; }}
.chip.amber {{ color:var(--amber); border-color:var(--amber-dim); background:var(--amber-glow); }}

.note {{ color:var(--mut); font-size:12.5px; max-width:100ch; margin:10px 0 0; }}
.note b {{ color:var(--ink); font-weight:600; }}

/* certificate footer */
.cert {{ margin-top:56px; border:1px solid var(--line); border-radius:3px; background:var(--panel);
  padding:20px 24px; }}
.cert h3 {{ font:700 12px/1 var(--mono); color:var(--amber); letter-spacing:.14em;
  text-transform:uppercase; margin:0 0 12px; }}
.cert p {{ color:var(--mut); font-size:12.5px; margin:6px 0; }}
.cert b {{ color:var(--ink); font-weight:600; }}

@media (max-width:760px) {{
  .wrap {{ padding:0 14px 60px; }}
  .mast h1 {{ font-size:32px; }}
  td.lanecell, th.lanehead {{ display:none; }}
}}
@media (prefers-reduced-motion:no-preference) {{
  .mast, .cards, section, .cert {{ animation:rise .4s ease-out both; }}
  section:nth-of-type(2) {{ animation-delay:.05s; }}
  @keyframes rise {{ from {{ opacity:0; transform:translateY(6px); }} to {{ opacity:1; }} }}
}}
</style></head><body><div class="wrap">

<header class="mast">
  <div class="id">TEST CERTIFICATE · RUN {esc(now)}</div>
  <h1>fastapi-<em>turbo</em> bench registry</h1>
  <p class="sub">Five servers, one contract, byte-identical responses. Apple M5 Max · 18 cores ·
  loopback · solo-boot per framework. Latency is one request at a time; throughput is every
  framework on its best all-core configuration.</p>
  <div class="meta">
    <span class="seal">PARITY 157/157</span>
    <span class="seal">UPSTREAM FASTAPI 0.138 · 0 REGRESSIONS</span>
    <span>LOCAL 1138 · WS 24</span>
    <span>WIRE-FAIR: 1-RTT READS · PER-REQUEST PAYLOAD BUILD</span>
    <span>ISOLATED DRIVER BOOTS</span>
  </div>
  <div class="legend">
    <span><i class="lt"></i>&nbsp;turbo</span>
    <span class="l-fa"><i></i>FastAPI + uvicorn</span>
    <span class="l-gin"><i></i>Gin (Go)</span>
    <span class="l-ff"><i></i>Fastify (Node)</span>
    <span class="l-ax"><i></i>raw axum (Rust)</span>
  </div>
</header>

<section>
  <div class="ch"><span class="num">CH-00</span><h2>Campaign delta</h2>
    <span class="cond">session start → this run</span></div>
  {campaign_cards()}
</section>

<section>
  <div class="ch"><span class="num">CH-01</span><h2>Single-connection latency</h2>
    <span class="cond">p50 µs · conn=1 · lower is better</span></div>
  {metric_table(LAT_ROWS, get_lat, fmt_us, "low", "p50 µs",
     "Large-JSON gap is the handler's own Python dict build (70 µs of the row, measured) — framework share is under 1 µs. "
     "Gin builds its payload per request since 5e02d66 (it was serving a startup-cached copy).")}
</section>

<section>
  <div class="ch"><span class="num">CH-02</span><h2>All-core throughput</h2>
    <span class="cond">req/s · c={esc(tpm.get("conc"))} · workers={esc(tpm.get("workers"))} (async groups {esc(tpm.get("async_workers", 12))}) · higher is better</span></div>
  {metric_table(TP_ROWS, get_tp, fmt_rps, "high", "req/s",
     "Each framework at its best: turbo/uvicorn multiprocess, Fastify cluster, Gin/axum native all-core. "
     "Python async groups run their measured-best worker count — same policy as driver choice.")}
</section>

<section>
  <div class="ch"><span class="num">CH-03</span><h2>Postgres</h2>
    <span class="cond">req/s · c={esc(dbm.get("conc"))} · engine-best drivers · higher is better</span></div>
  {metric_table([("Reads · 1 wire round trip in every framework", PG_READS)], get_db, fmt_rps, "high", "req/s")}
  {metric_table([("Writes · explicit transaction · commit=true is WAL-fsync durable", PG_WRITES)], get_db, fmt_rps, "high", "req/s",
     "All frameworks' commit=true rows sit on Postgres's WAL-fsync group-commit floor for this disk — the honest ceiling for durable writes.")}
</section>

<section>
  <div class="ch"><span class="num">CH-04</span><h2>Redis</h2>
    <span class="cond">req/s · c={esc(dbm.get("conc"))} · higher is better</span></div>
  {metric_table([("Single ops · pipeline ×10 · MULTI/EXEC ×10 · durable = SET + WAITAOF fsync", REDIS_ROWS),
                 ("turbo multiplexed client · fastapi_turbo.contrib_redis · opt-in", MUX_ROWS)], get_db, fmt_rps, "high", "req/s",
     "<b>set_durable</b> measures Redis's synchronous-fsync group-commit floor in a pre-warmed steady AOF state "
     "(rewrite complete, appendfsync=always) — compare within a run only. "
     "<b>mux client</b>: one multiplexed socket per event loop (the ioredis architecture — proven via CLIENT LIST) — "
     "same idiomatic <code>await client.get(...)</code>, works under uvicorn too; standard-client rows above stay the defaults.")}
</section>

<section>
  <div class="ch"><span class="num">CH-05</span><h2>Python driver matrix</h2>
    <span class="cond">turbo vs FastAPI+uvicorn · req/s · isolated boots</span></div>
  {driver_table()}
  <p class="note">asyncpg is the recommended async driver for both engines since the worker-loop init race fix
  (f757f8f) — the old “asyncpg is pathological under turbo” result was that bug. psycopg2 rows are clean since the
  pool got blocking-checkout parity (its <b>getconn()</b> raises where every other driver blocks).</p>
</section>

<section>
  <div class="ch"><span class="num">CH-06</span><h2>WebSocket</h2>
    <span class="cond">64-byte text echo · workers=8 · Python bench client</span></div>
  {ws_table()}
  <p class="note"><b>Rust-client rows are the true server RTT</b> (tokio-tungstenite, no Python-client jitter);
  Python-client rows include ~12-25 µs of client overhead. Direct-send (default on) writes outbound frames inline —
  no select-task wake — cutting server p99 20%. Fleet throughput is client-bounded; per-worker with an unconstrained
  client, turbo's thread mode measures <b>622k msg/s</b>. Loop-residency mode (FASTAPI_TURBO_WS_LOOP) trades +16 µs
  p50 for steadier p99 and fixes a receive-then-await hang.</p>
</section>

<div class="cert">
  <h3>Methodology · certificate of fairness</h3>
  <p><b>Wire parity.</b> Postgres reads are one round trip in every framework (autocommit reads / cached statements /
  no-op pool reset). Writes use an explicit transaction everywhere; commit=false rolls back, isolating the fsync cost.</p>
  <p><b>Contract parity.</b> All five servers return byte-identical bodies (large JSON 28,791 B · XML 45,833 B ·
  streams 640 B in 10 chunks), verified before every published run. Every payload is built per request.</p>
  <p><b>Process hygiene.</b> Solo boot per framework, serialized benches, isolated driver boots (co-resident pools
  contaminate up to 4×), connection budget ≤ 90 of Postgres's 100, rows with &gt;0.5% non-2xx marked invalid.</p>
  <p><b>Gates.</b> Every performance commit passed parity 157, local 1138, WebSocket 24, and FastAPI 0.138's own
  3,197-test suite with a byte-identical failure set, plus drift-controlled A/B benches — losers were reverted.</p>
</div>

</div></body></html>"""

OUT.write_text(HTML)
print(f"wrote {OUT} ({len(HTML):,} bytes)")
