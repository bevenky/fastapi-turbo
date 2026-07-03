"""Render results_matrix.json → a thorough cross-framework HTML report.

Per endpoint (row) and per framework (column) shows conn=1 p50/p99 (µs) and
conn=8 throughput (req/s). Best value in each row is highlighted; turbo is
flagged win/lose vs the best non-turbo framework.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results_matrix.json"
OUT = HERE / "report.html"

# column order (turbo highlighted)
FW_ORDER = ["fastapi-turbo", "FastAPI (uvicorn)", "Gin (Go)", "Fastify (Node)", "raw-axum"]

CATEGORIES = [
    ("baseline", "Baseline (static)"),
    ("simple", "Simple JSON — GET sync/async"),
    ("json", "Large JSON (1000 items)"),
    ("post", "POST (Pydantic body validation)"),
    ("put", "PUT"),
    ("patch", "PATCH"),
    ("delete", "DELETE"),
    ("xml", "XML response (small / large)"),
    ("streaming", "Streaming (10 chunks)"),
    ("redis", "Redis (get / set · sync / async)"),
    ("postgres", "Postgres (item-by-id / list · sync / async)"),
]


def fmt(v, suffix=""):
    if v is None:
        return '<span class="na">—</span>'
    if isinstance(v, float):
        return f"{v:,.0f}{suffix}"
    return f"{v:,}{suffix}"


def cell(row, metric):
    return None if not row else row.get(metric)


def main():
    data = json.loads(RESULTS.read_text())
    frameworks = [f for f in FW_ORDER if f in data] + [f for f in data if f not in FW_ORDER]

    # discover endpoint labels + their order from whichever framework has the most
    labels, meta = [], {}
    for fw in frameworks:
        for label, row in data[fw].items():
            if label not in meta:
                meta[label] = {"category": row.get("category", "other"),
                               "method": row.get("method", ""), "path": row.get("path", "")}
                labels.append(label)

    def rows_for(cat):
        return [l for l in labels if meta[l]["category"] == cat]

    html = []
    html.append(f"""<!doctype html><html><head><meta charset="utf-8">
<title>fastapi-turbo — cross-framework benchmark matrix</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0d1117;color:#e6edf3}}
 .wrap{{max-width:1500px;margin:0 auto;padding:28px}}
 h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:17px;margin:30px 0 8px;color:#58a6ff;border-bottom:1px solid #30363d;padding-bottom:5px}}
 .sub{{color:#8b949e;margin:0 0 18px}}
 table{{border-collapse:collapse;width:100%;margin:6px 0 4px;font-variant-numeric:tabular-nums}}
 th,td{{padding:6px 9px;text-align:right;border-bottom:1px solid #21262d}}
 th{{color:#8b949e;font-weight:600;text-align:right;background:#161b22;position:sticky;top:0}}
 td.lab,th.lab{{text-align:left;color:#e6edf3;white-space:nowrap}}
 .turbo{{background:#11261b}} th.turbo{{color:#3fb950}}
 .best{{color:#3fb950;font-weight:700}}
 .na{{color:#484f58}}
 .grp{{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}}
 .leg{{color:#8b949e;font-size:12px;margin:2px 0 24px}}
 .metric{{font-size:11px;color:#6e7681}}
 code{{color:#79c0ff}}
</style></head><body><div class="wrap">
<h1>fastapi-turbo — cross-framework benchmark matrix</h1>
<p class="sub">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · loopback · solo-boot per framework, workers=1 ·
conn=1 latency via <code>fastapi-turbo-bench</code> ({fmt_n()} reqs) · conn=8 throughput via <code>oha</code>.
Lower µs / higher req/s is better. <span class="best">Green</span> = best in row.</p>
""")

    def metric_table(cat_label, labels_in_cat, metric, suffix, better):
        if not labels_in_cat:
            return ""
        out = [f'<div class="grp">{metric_title(metric)}</div>', '<table><tr><th class="lab">endpoint</th>']
        for fw in frameworks:
            cls = "turbo" if fw == "fastapi-turbo" else ""
            out.append(f'<th class="{cls}">{fw}</th>')
        out.append("</tr>")
        for l in labels_in_cat:
            vals = {fw: cell(data[fw].get(l), metric) for fw in frameworks}
            present = [v for v in vals.values() if v is not None]
            best = (min if better == "low" else max)(present) if present else None
            meth = meta[l]["method"]
            out.append(f'<tr><td class="lab">{l} <span class="metric">{meth}</span></td>')
            for fw in frameworks:
                v = vals[fw]
                cls = "turbo " if fw == "fastapi-turbo" else ""
                if v is not None and best is not None and v == best:
                    cls += "best"
                out.append(f'<td class="{cls}">{fmt(v, suffix)}</td>')
            out.append("</tr>")
        out.append("</table>")
        return "".join(out)

    for cat, cat_label in CATEGORIES:
        lic = rows_for(cat)
        if not lic:
            continue
        html.append(f"<h2>{cat_label}</h2>")
        html.append(metric_table(cat_label, lic, "p50_us", "µs", "low"))
        html.append(metric_table(cat_label, lic, "p99_us", "µs", "low"))
        html.append(metric_table(cat_label, lic, "rps_c8", "", "high"))

    html.append("</div></body></html>")
    OUT.write_text("".join(html))
    print(f"wrote {OUT}")


def fmt_n():
    return "10k"


def metric_title(metric):
    return {"p50_us": "conn=1 · p50 latency (µs, lower better)",
            "p99_us": "conn=1 · p99 latency (µs, lower better)",
            "rps_c8": "conn=8 · throughput (req/s, higher better)"}[metric]


if __name__ == "__main__":
    main()
