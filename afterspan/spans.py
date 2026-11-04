"""Span records: one per MCP (or tool) call, shaped after the OpenTelemetry MCP semantic conventions.

A span is a plain dict so it round-trips through JSONL without a schema library:
  name            "{mcp.method.name} {tool}" (e.g. "tools/call search")
  trace_id        32 hex chars, one per agent session
  span_id         16 hex chars
  start_ns        unix nanoseconds
  end_ns          unix nanoseconds, or None when no result was ever seen
  status          "ok" | "error" | "unfinished"
  source          "claude-code" | "codex" | "tap"
  attributes      OTel attribute names (mcp.method.name, gen_ai.tool.name, mcp.server.name, error.type, ...)
Argument and result *values* are never stored: only argument key names and byte sizes, so a span file is safe to share.
"""
import datetime as dt
import hashlib
import json
import os
import re

SECRETISH = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer|cookie|session)")


def hex_id(*parts, n=16):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:n]


def ts_ns(stamp):
    """ISO-8601 (with Z or offset) or epoch seconds -> unix ns; None if unreadable."""
    if stamp is None:
        return None
    if isinstance(stamp, (int, float)):
        return int(stamp * 1e9)
    try:
        return int(dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp() * 1e9)
    except ValueError:
        return None


def size(v):
    if v is None:
        return 0
    return len(v.encode() if isinstance(v, str) else json.dumps(v, default=str).encode())


def arg_keys(args):
    """Top-level argument names only; secret-looking names are kept as a count, not a name."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return []
    if not isinstance(args, dict):
        return []
    keys = sorted(k for k in args if not SECRETISH.search(k))
    hidden = len(args) - len(keys)
    return keys + ([f"<{hidden} hidden>"] if hidden else [])


def split_tool(full):
    """'mcp__parallel-search__web_search' -> ('parallel-search', 'web_search'); 'Bash' -> (None, 'Bash')."""
    if full.startswith("mcp__"):
        rest = full[5:]
        server, _, tool = rest.partition("__")
        return (server, tool) if tool else (None, rest)
    return None, full


def error_type(text):
    """A short, value-free label for an error result."""
    t = (text or "")[:600].lower()
    for label, pat in (("rate_limit", r"rate.?limit|429|too many requests|quota"), ("timeout", r"time(d)? ?out|deadline"),
                       ("auth", r"401|403|unauthori[sz]ed|forbidden|invalid api key|not authenticated|login"),
                       ("not_found", r"404|not found|no such"), ("server_error", r"5\d\d|internal server error|bad gateway"),
                       ("connection", r"connection|econnrefused|network|dns|socket"),
                       ("validation", r"invalid|validation|schema|required|must be|expected")):
        if re.search(pat, t):
            return label
    return "tool_error"


def make(trace_id, call_id, tool_full, start_ns, end_ns, *, source, args=None, result=None, is_error=False,
         method="tools/call", session=None, server=None):
    srv, tool = split_tool(tool_full)
    srv = server or srv
    attrs = {"mcp.method.name": method, "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool,
             "gen_ai.tool.call.id": call_id, "mcp.request.bytes": size(args), "mcp.response.bytes": size(result),
             "mcp.request.argument_keys": arg_keys(args)}
    if srv:
        attrs["mcp.server.name"] = srv
    if session:
        attrs["mcp.session.id"] = session
    status = "unfinished" if end_ns is None else "error" if is_error else "ok"
    if status == "error":
        attrs["error.type"] = error_type(result if isinstance(result, str) else json.dumps(result, default=str))
    return {"name": f"{method} {tool}", "trace_id": trace_id, "span_id": hex_id(trace_id, call_id), "start_ns": start_ns,
            "end_ns": end_ns, "status": status, "source": source, "attributes": attrs}


def duration_ms(s):
    return None if s.get("end_ns") is None or s.get("start_ns") is None else max(0.0, (s["end_ns"] - s["start_ns"]) / 1e6)


def write(spans, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    n = 0
    with open(path, "a") as f:
        for s in spans:
            f.write(json.dumps(s, separators=(",", ":")) + "\n"); n += 1
    return n


def read(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
