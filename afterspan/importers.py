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


def claude_code(root="~/.claude/projects", days=30, mcp_only=True, harness=False):
    seen, out = set(), []
    for f in _files(root, "**/*.jsonl", days):
        session = os.path.splitext(os.path.basename(f))[0]
        trace = S.hex_id("claude-code", session, n=32)
        pending = {}
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
                    if c.get("type") == "tool_use" and c.get("id") not in seen:
                        name = c.get("name") or ""
                        if not _keep(name, mcp_only, harness):
                            continue
                        pending[c["id"]] = (name, t, c.get("input"))
                    elif c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                        cid = c["tool_use_id"]
                        name, t0, args = pending.pop(cid)
                        seen.add(cid)
                        out.append(S.make(trace, cid, name, t0, t, source="claude-code", args=args,
                                          result=_text(c.get("content")), is_error=bool(c.get("is_error")), session=session))
        for cid, (name, t0, args) in pending.items():
            if cid not in seen:
                seen.add(cid)
                out.append(S.make(trace, cid, name, t0, None, source="claude-code", args=args, session=session))
    return _recent(out, days)


def codex(root="~/.codex/sessions", days=30, mcp_only=True, harness=False):
    seen, out = set(), []
    for f in _files(root, "**/*.jsonl", days):
        session = os.path.splitext(os.path.basename(f))[0]
        trace = S.hex_id("codex", session, n=32)
        pending = {}
        try:
            lines = open(f, errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                if '"function_call' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload") or {}
                t = S.ts_ns(d.get("timestamp"))
                if p.get("type") == "function_call" and p.get("call_id") not in seen:
                    ns = p.get("namespace") or ""
                    full = f"{ns}__{p.get('name')}" if ns.startswith("mcp__") else (p.get("name") or "")
                    if not _keep(full, mcp_only, harness):
                        continue
                    pending[p["call_id"]] = (full, t, p.get("arguments"))
                elif p.get("type") == "function_call_output" and p.get("call_id") in pending:
                    cid = p["call_id"]
                    full, t0, args = pending.pop(cid)
                    seen.add(cid)
                    text = _text(p.get("output"))
                    err = text.lstrip().lower().startswith(("error", "tool call error", "mcp error")) or '"is_error": true' in text
                    out.append(S.make(trace, cid, full, t0, t, source="codex", args=args, result=text, is_error=err, session=session))
        for cid, (full, t0, args) in pending.items():
            if cid not in seen:
                seen.add(cid)
                out.append(S.make(trace, cid, full, t0, None, source="codex", args=args, session=session))
    return _recent(out, days)
