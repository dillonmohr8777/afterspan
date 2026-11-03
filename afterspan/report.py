"""Aggregate spans into a per-server and per-tool health table (text, JSON or a single-file HTML page)."""
import collections
import datetime as dt
import html
import json

from .spans import duration_ms


def pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)


def _row(key, group):
    durs = [d for d in (duration_ms(s) for s in group) if d is not None]
    errs = [s for s in group if s["status"] == "error"]
    kinds = collections.Counter(s["attributes"].get("error.type", "tool_error") for s in errs)
    return {"key": key, "calls": len(group), "errors": len(errs), "error_rate": round(len(errs) / len(group), 3),
            "unfinished": sum(1 for s in group if s["status"] == "unfinished"),
            "p50_ms": pct(durs, 0.5), "p95_ms": pct(durs, 0.95), "max_ms": round(max(durs), 1) if durs else None,
            "sessions": len({s["trace_id"] for s in group}), "error_types": dict(kinds.most_common(4)),
            "response_kb": round(sum(s["attributes"].get("mcp.response.bytes", 0) for s in group) / 1024, 1)}


def summarize(spans, by="server", aliases=None):
    spans = list(spans)
    aliases = aliases or {}
    groups = collections.defaultdict(list)
    for s in spans:
        a = s["attributes"]
        srv = a.get("mcp.server.name") or "(built-in)"
        srv = aliases.get(srv, srv)
        groups[srv if by == "server" else f"{srv} / {a.get('gen_ai.tool.name')}"].append(s)
    rows = sorted((_row(k, g) for k, g in groups.items()), key=lambda r: -r["calls"])
    starts = [s["start_ns"] for s in spans if s.get("start_ns")]
    return {"spans": len(spans), "sessions": len({s["trace_id"] for s in spans}),
            "sources": dict(collections.Counter(s["source"] for s in spans)),
            "from": dt.datetime.fromtimestamp(min(starts) / 1e9).isoformat(timespec="minutes") if starts else None,
            "to": dt.datetime.fromtimestamp(max(starts) / 1e9).isoformat(timespec="minutes") if starts else None,
            "by": by, "rows": rows}


def worst(summary, n=5, min_calls=10):
    """Rows most worth fixing: error count weighted by rate, then slow p95."""
    rows = [r for r in summary["rows"] if r["calls"] >= min_calls]
    return sorted(rows, key=lambda r: (-(r["errors"] * (0.5 + r["error_rate"])), -(r["p95_ms"] or 0)))[:n]


def text(summary, limit=25):
    w = max([len(r["key"]) for r in summary["rows"][:limit]] + [6])
    out = [f"{summary['spans']} calls in {summary['sessions']} sessions, {summary['from']} to {summary['to']} ({summary['sources']})",
           f"{'name':<{w}}  {'calls':>6} {'err%':>6} {'p50 ms':>8} {'p95 ms':>9} {'max ms':>9}  errors"]
    for r in summary["rows"][:limit]:
        f = lambda v: "-" if v is None else f"{v:,.0f}"
        out.append(f"{r['key']:<{w}}  {r['calls']:>6} {r['error_rate'] * 100:>5.1f}% {f(r['p50_ms']):>8} {f(r['p95_ms']):>9} "
                   f"{f(r['max_ms']):>9}  {', '.join(f'{k} {v}' for k, v in r['error_types'].items())}")
    return "\n".join(out)


CSS = """:root{--bg:#fbfaf7;--fg:#1d1c1a;--mute:#6b675f;--line:#e4e0d6;--bad:#b3261e;--warn:#8a5a00;--ok:#1e6b3a;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#141412;--fg:#ecebe6;--mute:#a19d93;--line:#2c2b27;--bad:#ff8a80;--warn:#f0c060;--ok:#7fd49a;color-scheme:dark}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif;margin:0;padding:32px 20px}
main{max-width:1100px;margin:0 auto}h1{font-size:28px;margin:0 0 4px}p.m{color:var(--mute);margin:0 0 24px}
.k{display:flex;gap:28px;flex-wrap:wrap;margin:0 0 28px}.k b{display:block;font-size:26px}.k span{color:var(--mute);font-size:13px}
.s{overflow-x:auto}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:right;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--mute);font-weight:600;font-size:13px}.bad{color:var(--bad);font-weight:600}.warn{color:var(--warn)}
h2{font-size:17px;margin:32px 0 10px}"""


def html_page(by_server, by_tool, title="afterspan"):
    def table(s):
        rows = []
        for r in s["rows"][:60]:
            er = r["error_rate"]
            cls = "bad" if er >= 0.1 else "warn" if er >= 0.03 else ""
            f = lambda v: "-" if v is None else f"{v:,.0f}"
            rows.append(f"<tr><td>{html.escape(r['key'])}</td><td>{r['calls']:,}</td><td class='{cls}'>{er * 100:.1f}%</td>"
                        f"<td>{f(r['p50_ms'])}</td><td>{f(r['p95_ms'])}</td><td>{f(r['max_ms'])}</td><td>{r['sessions']:,}</td>"
                        f"<td style='text-align:left'>{html.escape(', '.join(f'{k} {v}' for k, v in r['error_types'].items()))}</td></tr>")
        return ("<div class='s'><table><thead><tr><th>name</th><th>calls</th><th>errors</th><th>p50 ms</th><th>p95 ms</th>"
                "<th>max ms</th><th>sessions</th><th style='text-align:left'>error types</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    errs = sum(r["errors"] for r in by_server["rows"])
    fix = worst(by_tool)
    fixes = "".join(f"<li><b>{html.escape(r['key'])}</b>: {r['errors']} errors in {r['calls']} calls "
                    f"({html.escape(', '.join(r['error_types']))}), p95 {r['p95_ms']:,.0f} ms</li>" for r in fix if r["errors"])
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style><main><h1>{html.escape(title)}</h1><p class=m>{by_server['from']} to {by_server['to']} · "
            f"rebuilt from agent transcripts and taps · {html.escape(json.dumps(by_server['sources']))}</p>"
            f"<div class=k><div><b>{by_server['spans']:,}</b><span>MCP calls</span></div><div><b>{by_server['sessions']:,}</b><span>sessions</span></div>"
            f"<div><b>{errs:,}</b><span>errors</span></div><div><b>{len(by_server['rows'])}</b><span>servers</span></div></div>"
            + (f"<h2>Fix first</h2><ul>{fixes}</ul>" if fixes else "")
            + f"<h2>By server</h2>{table(by_server)}<h2>By tool</h2>{table(by_tool)}</main>")
