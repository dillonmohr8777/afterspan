"""Rebuild spans from transcripts agents already wrote. Read-only: transcripts are never modified.

Claude Code: ~/.claude/projects/<project>/<session>.jsonl. An assistant line carries content items
  {"type": "tool_use", "id", "name", "input"}; a later user line carries {"type": "tool_result", "tool_use_id",
  "is_error", "content"}. Each line has a top-level "timestamp". Resumed and forked sessions copy earlier lines,
  so calls are de-duplicated by tool_use id.
Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. A "response_item" line with payload type "function_call"
  ({"name", "namespace": "mcp__<server>", "call_id", "arguments"}) pairs with a "function_call_output" ({"call_id",
  "output"}). MCP calls are the ones whose namespace starts with "mcp__".
Duration is result timestamp minus call timestamp: it is what the agent waited, including transport and queueing.
"""
import glob
import json
import os
import time

from . import spans as S


HARNESS = ("ccd_",)  # app/harness tools (plans, wrap-ups, session panes), not MCP servers; their waits are human time


def _keep(name, mcp_only, harness):
    if mcp_only and not name.startswith("mcp__"):
        return False
    return harness or not name[5:].startswith(HARNESS)


def _recent(found, days):
    """Resumed and forked sessions copy old lines into new files, so cut by each call's own start time too."""
    if not days:
        return found
    cut = (time.time() - days * 86400) * 1e9
    return [s for s in found if s.get("start_ns") and s["start_ns"] >= cut]


def _files(root, pattern, days):
    cut = time.time() - days * 86400 if days else 0
    return sorted(f for f in glob.glob(os.path.join(os.path.expanduser(root), pattern), recursive=True)
                  if os.path.getmtime(f) >= cut)


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return json.dumps(content, default=str) if content is not None else ""


def _flag_error(obj, depth=0):
    """True/False when a structured error flag is present, else None."""
    if isinstance(obj, str):
        s = obj.strip()
        if not s[:1] in ("{", "["):
            return None
        try:
            obj = json.loads(s)
        except ValueError:
            return None
    if isinstance(obj, list) and depth < 2:
        found = [_flag_error(x, depth + 1) for x in obj]
        return True if any(f is True for f in found) else (False if any(f is False for f in found) else None)
    if not isinstance(obj, dict):
        return None
    for key in ("isError", "is_error"):
        if key in obj and isinstance(obj[key], bool):
            return obj[key]
    if depth < 2:
        for key in ("result", "output", "content"):
            if key in obj:
                inner = _flag_error(obj[key], depth + 1)
                if inner is not None:
                    return inner
    return None


def _codex_error(raw, text):
    flag = _flag_error(raw)
    if flag is not None:
        return flag
    return " ".join(text.lstrip().lower().split()).startswith(("error", "tool call error", "mcp error"))


def _result(t, body, error):
    # Reconciliation may span many files. Retain measurements, never raw result bodies.
    return {"end_ns": t, "bytes": S.size(body), "error": S.error_type(body) if error else None}


def _reconcile(calls, results, days):
    """Pair across all files before deciding that a call never completed."""
    out = []
    for cid, span in calls.items():
        result = results.get(cid)
        if result:
            span["end_ns"] = result["end_ns"]
            span["attributes"]["mcp.response.bytes"] = result["bytes"]
            if result["end_ns"] is not None:
                span["status"] = "error" if result["error"] else "ok"
                if result["error"]:
                    span["attributes"]["error.type"] = result["error"]
        out.append(span)
    return _recent(out, days)


def claude_code(root="~/.claude/projects", days=30, mcp_only=True, harness=False):
    calls, results = {}, {}
    for f in _files(root, "**/*.jsonl", days):
        session = os.path.splitext(os.path.basename(f))[0]
        trace = S.hex_id("claude-code", session, n=32)
        try:
            lines = open(f, errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = S.ts_ns(d.get("timestamp"))
                content = (d.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use" and c.get("id") not in calls:
                        name = c.get("name") or ""
                        if not _keep(name, mcp_only, harness):
                            continue
                        calls[c["id"]] = S.make(trace, c["id"], name, t, None, source="claude-code",
                                               args=c.get("input"), session=session)
                    elif c.get("type") == "tool_result":
                        cid = c["tool_use_id"]
                        if cid not in results or (results[cid]["end_ns"] is None and t is not None):
                            results[cid] = _result(t, _text(c.get("content")), bool(c.get("is_error")))
    return _reconcile(calls, results, days)


def _item_duration_ns(item):
    for key in ("duration_ms", "durationMs"):
        v = item.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v * 1e6)
    d = item.get("duration")
    if isinstance(d, bool):
        return None
    if isinstance(d, (int, float)):
        return int(d * 1e6)
    if isinstance(d, dict) and isinstance(d.get("secs"), (int, float)):
        return int(d["secs"] * 1e9 + (d.get("nanos") or 0))
    return None


def _mcp_tool_call(item, t, trace, session, mcp_only, harness):
    """An event_msg item_completed McpToolCall -> (id, span, result), or None."""
    cid = item.get("id")
    if not cid:
        return None
    full = f"mcp__{item.get('server')}__{item.get('tool')}"
    if not _keep(full, mcp_only, harness):
        return None
    start = item.get("started_at_ms")
    start = int(start * 1e6) if isinstance(start, (int, float)) and not isinstance(start, bool) else t
    dur = _item_duration_ns(item)
    end = start + dur if start is not None and dur is not None else t
    res = item.get("result")
    if isinstance(res, dict) and isinstance(res.get("Ok"), dict):
        res = res["Ok"]
    err = item.get("status") == "failed" or (isinstance(res, dict) and res.get("isError") is True)
    body = _text(res.get("content") if isinstance(res, dict) and "content" in res else res)
    if err and item.get("error") is not None:
        body = f"{body} {_text(item['error'].get('message') if isinstance(item['error'], dict) else item['error'])}".strip()
    span = S.make(trace, cid, full, start, None, source="codex", args=item.get("arguments"), session=session)
    return cid, span, _result(end, body, err)


def codex(root="~/.codex/sessions", days=30, mcp_only=True, harness=False):
    calls, results = {}, {}
    items = {}  # McpToolCall events by id; their status takes precedence over function_call/output pairs
    for f in _files(root, "**/*.jsonl", days):
        session = os.path.splitext(os.path.basename(f))[0]
        trace = S.hex_id("codex", session, n=32)
        try:
            lines = open(f, errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                if '"function_call' not in line and "McpToolCall" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload") or {}
                if not isinstance(p, dict):
                    continue
                t = S.ts_ns(d.get("timestamp"))
                if p.get("type") == "item_completed":
                    item = p.get("item")
                    if isinstance(item, dict) and item.get("type") == "McpToolCall":
                        got = _mcp_tool_call(item, t, trace, session, mcp_only, harness)
                        if got and got[0] not in items:
                            items[got[0]] = got[1:]
                elif p.get("type") == "function_call" and p.get("call_id") not in calls:
                    ns = p.get("namespace") or ""
                    full = f"{ns}__{p.get('name')}" if ns.startswith("mcp__") else (p.get("name") or "")
                    if not _keep(full, mcp_only, harness):
                        continue
                    calls[p["call_id"]] = S.make(trace, p["call_id"], full, t, None, source="codex",
                                                args=p.get("arguments"), session=session)
                elif p.get("type") == "function_call_output":
                    cid = p["call_id"]
                    raw_out = p.get("output")
                    text = _text(raw_out)
                    err = _codex_error(raw_out, text)
                    if cid not in results or (results[cid]["end_ns"] is None and t is not None):
                        results[cid] = _result(t, text, err)
    for cid, (span, result) in items.items():
        calls[cid], results[cid] = span, result
    return _reconcile(calls, results, days)
