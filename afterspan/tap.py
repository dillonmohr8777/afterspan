"""Live stdio tap: run an MCP server behind afterspan and record a span per JSON-RPC request.

  afterspan tap --name my-server --out spans.jsonl -- python3 server.py
The client talks to afterspan exactly as it would to the server (newline-delimited JSON-RPC on stdin/stdout);
bytes are forwarded unchanged and stderr passes straight through. With --propagate, a W3C traceparent is added to
each request's params._meta (MCP SEP-414) so a server that reads it can join the trace.
"""
import json
import os
import subprocess
import sys
import threading
import time

from . import spans as S


def run(cmd, name, out, propagate=False):
    child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    assert child.stdin and child.stdout
    cin, cout = child.stdin, child.stdout
    trace = S.hex_id("tap", name, os.getpid(), time.time_ns(), n=32)
    pending, lock = {}, threading.Lock()
    sink = open(out, "a", buffering=1)

    def record(span):
        with lock:
            sink.write(json.dumps(span, separators=(",", ":")) + "\n")

    def upstream():  # client -> server
        for raw in sys.stdin.buffer:
            line = raw
            try:
                msgs = json.loads(raw)
                batch = isinstance(msgs, list)
                changed = False
                for m in (msgs if batch else [msgs]):
                    if isinstance(m, dict) and "method" in m and "id" in m:
                        params = m.get("params") or {}
                        tool = params.get("name") if m["method"] == "tools/call" else m["method"].split("/")[-1]
                        with lock:
                            pending[json.dumps(m["id"])] = (m["method"], tool, time.time_ns(), params.get("arguments"))
                        if propagate and isinstance(m.get("params", {}), dict):
                            m.setdefault("params", {}).setdefault("_meta", {})["traceparent"] = \
                                f"00-{trace}-{S.hex_id(trace, m['id'])}-01"
                            changed = True
                if changed:
                    line = (json.dumps(msgs, separators=(",", ":")) + "\n").encode()
            except (ValueError, TypeError):
                pass
            cin.write(line); cin.flush()
        cin.close()

    def downstream():  # server -> client
        for raw in cout:
            sys.stdout.buffer.write(raw); sys.stdout.buffer.flush()
            try:
                msgs = json.loads(raw)
            except ValueError:
                continue
            for m in (msgs if isinstance(msgs, list) else [msgs]):
                if not isinstance(m, dict) or "id" not in m or "method" in m:
                    continue
                with lock:
                    hit = pending.pop(json.dumps(m["id"]), None)
                if not hit:
                    continue
                method, tool, t0, args = hit
                res = m.get("result")
                err = "error" in m or (isinstance(res, dict) and res.get("isError") is True)
                body = m.get("error") if "error" in m else res
                record(S.make(trace, str(m["id"]), tool or method, t0, time.time_ns(), source="tap", args=args,
                              result=body, is_error=err, method=method, server=name))

    up = threading.Thread(target=upstream, daemon=True)
    down = threading.Thread(target=downstream, daemon=True)
    up.start(); down.start()
    rc = child.wait()
    down.join(timeout=2)
    with lock:
        for k, (method, tool, t0, args) in pending.items():
            sink.write(json.dumps(S.make(trace, k, tool or method, t0, None, source="tap", args=args, method=method,
                                         server=name), separators=(",", ":")) + "\n")
    sink.close()
    return rc
