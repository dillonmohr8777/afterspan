"""afterspan tests: synthetic transcripts, a fake MCP server behind the tap, a local OTLP collector. No network."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from afterspan import importers, otlp, report, spans as S  # noqa: E402

NOW = time.time()


def iso(sec):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec)) + f".{int(sec * 1000) % 1000:03d}Z"


class Transcripts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def write(self, rel, lines):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        return p

    def cc_pair(self, tid, name, t0, t1, args, result, err=False):
        return [{"timestamp": iso(t0), "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": args}]}},
                {"timestamp": iso(t1), "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": err,
                                                                 "content": result}]}}]

    def test_claude_code_pairs_dedupes_and_hides_values(self):
        a = self.cc_pair("t1", "mcp__search__web_search", NOW - 60, NOW - 58.5, {"query": "x", "api_key": "sk-123"}, "ok")
        b = self.cc_pair("t2", "mcp__search__web_search", NOW - 50, NOW - 49, {"query": "y"}, "Error: 429 Too Many Requests", err=True)
        c = self.cc_pair("t3", "Bash", NOW - 40, NOW - 39, {"command": "ls"}, "files")
        h = self.cc_pair("t4", "mcp__ccd_turn__update_plan", NOW - 30, NOW - 20, {}, "ok")
        self.write("proj/s1.jsonl", a + b + c + h)
        self.write("proj/s1-resumed.jsonl", a)  # a resumed session repeats t1
        out = importers.claude_code(self.dir, days=1)
        self.assertEqual(sorted(s["attributes"]["gen_ai.tool.call.id"] for s in out), ["t1", "t2"])
        ok = next(s for s in out if s["attributes"]["gen_ai.tool.call.id"] == "t1")
        self.assertEqual(ok["attributes"]["mcp.server.name"], "search")
        self.assertAlmostEqual(S.duration_ms(ok), 1500, delta=2)
        self.assertEqual(ok["attributes"]["mcp.request.argument_keys"], ["query", "<1 hidden>"])
        self.assertNotIn("sk-123", json.dumps(out))
        bad = next(s for s in out if s["status"] == "error")
        self.assertEqual(bad["attributes"]["error.type"], "rate_limit")
        self.assertEqual(len(importers.claude_code(self.dir, days=1, mcp_only=False)), 3)  # + Bash, harness still hidden
        self.assertEqual(len(importers.claude_code(self.dir, days=1, harness=True)), 3)

    def test_old_calls_in_recent_files_are_cut(self):
        self.write("proj/old.jsonl", self.cc_pair("o1", "mcp__a__b", NOW - 40 * 86400, NOW - 40 * 86400 + 1, {}, "ok"))
        self.assertEqual(importers.claude_code(self.dir, days=30), [])

    def test_unfinished_call(self):
        self.write("proj/u.jsonl", self.cc_pair("u1", "mcp__a__b", NOW - 9, NOW - 8, {}, "ok")[:1])
        (s,) = importers.claude_code(self.dir, days=1)
        self.assertEqual(s["status"], "unfinished")
        self.assertIsNone(S.duration_ms(s))

    def test_resumed_claude_completes_original_call(self):
        pair = self.cc_pair("resume", "mcp__a__b", NOW - 9, NOW - 6, {}, "timeout", err=True)
        self.write("proj/a.jsonl", pair[:1])
        self.write("proj/z.jsonl", pair)
        (s,) = importers.claude_code(self.dir, days=1)
        self.assertEqual(s["status"], "error")
        self.assertEqual(s["attributes"]["mcp.session.id"], "a")
        self.assertAlmostEqual(S.duration_ms(s), 3000, delta=2)

    def test_output_only_resume_pairs_in_either_file_order(self):
        pair = self.cc_pair("resume", "mcp__a__b", NOW - 9, NOW - 6, {}, "ok")
        self.write("proj/a.jsonl", pair[1:])
        self.write("proj/z.jsonl", pair[:1])
        (s,) = importers.claude_code(self.dir, days=1)
        self.assertEqual(s["status"], "ok")
        self.assertAlmostEqual(S.duration_ms(s), 3000, delta=2)

    def test_resumed_codex_completes_original_call(self):
        call = {"timestamp": iso(NOW - 9), "type": "response_item", "payload": {
            "type": "function_call", "name": "run", "namespace": "mcp__a", "call_id": "resume", "arguments": "{}"}}
        result = {"timestamp": iso(NOW - 6), "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "resume", "output": "Error: timeout"}}
        self.write("a.jsonl", [call])
        self.write("z.jsonl", [call, result])
        (s,) = importers.codex(self.dir, days=1)
        self.assertEqual(s["status"], "error")
        self.assertAlmostEqual(S.duration_ms(s), 3000, delta=2)

    def test_codex(self):
        self.write("2026/11/03/rollout-x.jsonl", [
            {"timestamp": iso(NOW - 20), "type": "response_item", "payload": {"type": "function_call", "name": "js",
             "namespace": "mcp__repl", "call_id": "c1", "arguments": "{\"code\": \"1\"}"}},
            {"timestamp": iso(NOW - 17), "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_text", "text": "Wall time: 3s"}]}},
            {"timestamp": iso(NOW - 15), "type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "c2",
             "arguments": "{}"}}])
        (s,) = importers.codex(self.dir, days=1)
        self.assertEqual((s["attributes"]["mcp.server.name"], s["attributes"]["gen_ai.tool.name"], s["status"]), ("repl", "js", "ok"))
        self.assertEqual(s["attributes"]["mcp.request.argument_keys"], ["code"])

    def test_codex_mcp_tool_call_events(self):
        def done(cid, tool, status, text, start, dur_ms):
            return {"timestamp": iso(start + dur_ms / 1000), "type": "event_msg", "payload": {
                "type": "item_completed", "item": {
                    "type": "McpToolCall", "id": cid, "server": "computer-use", "tool": tool, "status": status,
                    "arguments": {"app": "x"}, "started_at_ms": int(start * 1000), "duration_ms": dur_ms,
                    "result": {"content": [{"type": "text", "text": text}]}}}}
        self.write("2026/11/03/rollout-y.jsonl", [
            {"timestamp": iso(NOW - 20), "type": "response_item", "payload": {
                "type": "function_call", "name": "click", "namespace": "mcp__computer-use", "call_id": "m1", "arguments": "{}"}},
            {"timestamp": iso(NOW - 18), "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "m1", "output": [{"type": "input_text", "text": "Wall time: 2s"}]}},
            done("m1", "click", "failed", "Computer Use server error -10005: timeoutReached", NOW - 20, 2000),
            done("m2", "list_apps", "completed", "ok", NOW - 15, 500)])
        out = {s["attributes"]["gen_ai.tool.call.id"]: s for s in importers.codex(self.dir, days=1)}
        self.assertEqual(sorted(out), ["m1", "m2"])
        self.assertEqual((out["m1"]["status"], out["m1"]["attributes"]["error.type"]), ("error", "timeout"))
        self.assertEqual(out["m1"]["attributes"]["mcp.server.name"], "computer-use")
        self.assertEqual(out["m1"]["attributes"]["gen_ai.tool.name"], "click")
        self.assertAlmostEqual(S.duration_ms(out["m1"]), 2000, delta=2)
        self.assertEqual(out["m2"]["status"], "ok")


class Report(unittest.TestCase):
    def test_percentiles_and_worst(self):
        base = int(NOW * 1e9)
        rows = [S.make("t" * 32, f"c{i}", "mcp__srv__tool", base, base + (i + 1) * 1_000_000, source="tap",
                       is_error=i < 3, result="timeout") for i in range(20)]
        s = report.summarize(rows, "tool", {"srv": "server"})
        (r,) = s["rows"]
        self.assertEqual((r["key"], r["calls"], r["errors"], r["p50_ms"]), ("server / tool", 20, 3, 10.5))
        self.assertEqual(r["error_types"], {"timeout": 3})
        self.assertEqual(report.worst(s)[0]["key"], "server / tool")
        page = report.html_page(report.summarize(rows), s)
        self.assertIn("Fix first", page)
        self.assertIn("prefers-color-scheme:dark", page)

    def test_fix_first_without_p95(self):
        rows = [S.make("t" * 32, f"c{i}", "mcp__srv__tool", None, int(NOW * 1e9), source="codex", is_error=i == 0, result="x")
                for i in range(10)]
        page = report.html_page(report.summarize(rows), report.summarize(rows, "tool"))
        self.assertIn("p95 -</li>", page)


FAKE_SERVER = r'''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    if m.get("params", {}).get("name") == "boom":
        out = {"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32000, "message": "upstream 503"}}
    else:
        out = {"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": "hi"}],
               "meta_seen": "traceparent" in (m.get("params", {}).get("_meta") or {})}}
    sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush()
'''


class Tap(unittest.TestCase):
    def test_server_can_exit_while_client_keeps_input_open(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "spans.jsonl")
        server = 'import json,sys; m=json.loads(sys.stdin.readline()); print(json.dumps({"id":m["id"],"result":"ok"}),flush=True)'
        p = subprocess.Popen([sys.executable, "-m", "afterspan", "tap", "--name", "fake", "--out", out,
                              "--", sys.executable, "-c", server], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=ROOT)
        p.stdin.write(b'{"id":1,"method":"tools/call","params":{"name":"echo"}}\n'); p.stdin.flush()
        try:
            rc = p.wait(timeout=10)
        finally:
            p.stdin.close(); p.stdin = None
        stdout, stderr = p.communicate(timeout=10)
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(json.loads(stdout)["result"], "ok")
        self.assertEqual(list(S.read(out))[0]["status"], "ok")

    def test_typed_ids_and_propagation_match_recorded_spans(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "spans.jsonl")
        server = '''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 print(json.dumps({"id":m["id"],"result":{"traceparent":m["params"]["_meta"]["traceparent"]}}),flush=True)
'''
        msgs = [{"id": ident, "method": "tools/call", "params": {"name": "echo"}} for ident in [1, "1"]]
        p = subprocess.run([sys.executable, "-m", "afterspan", "tap", "--name", "fake", "--out", out,
                            "--propagate", "--", sys.executable, "-c", server],
                           input="".join(json.dumps(m) + "\n" for m in msgs), text=True, capture_output=True,
                           timeout=15, cwd=ROOT)
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = list(S.read(out))
        replies = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual(len({s["span_id"] for s in rows}), 2)
        self.assertEqual([r["result"]["traceparent"].split("-")[2] for r in replies], [s["span_id"] for s in rows])

    def test_final_response_survives_backpressure_longer_than_two_seconds(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "spans.jsonl")
        server = 'import json,sys; m=json.loads(sys.stdin.readline()); print(json.dumps({"id":m["id"],"result":"x"*262144}),flush=True)'
        p = subprocess.Popen([sys.executable, "-m", "afterspan", "tap", "--name", "fake", "--out", out,
                              "--", sys.executable, "-c", server], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=ROOT)
        p.stdin.write(b'{"id":1,"method":"tools/call","params":{"name":"echo"}}\n')
        p.stdin.close(); p.stdin = None
        time.sleep(2.4)  # Simulate a client that temporarily stops draining its stdout pipe.
        stdout, stderr = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0, stderr)
        self.assertEqual(len(json.loads(stdout)["result"]), 262144)
        (s,) = S.read(out)
        self.assertEqual(s["status"], "ok")
        self.assertLess(S.duration_ms(s), 2000)

    def test_tap_forwards_and_records(self):
        d = tempfile.mkdtemp()
        srv, out = os.path.join(d, "srv.py"), os.path.join(d, "spans.jsonl")
        with open(srv, "w") as f:
            f.write(FAKE_SERVER)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"q": "x"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": "b", "method": "tools/call", "params": {"name": "boom", "arguments": {}}}]
        p = subprocess.run([sys.executable, "-m", "afterspan", "tap", "--name", "fake", "--out", out, "--propagate", "--",
                            sys.executable, srv], input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True,
                           text=True, timeout=30, cwd=ROOT)
        replies = [json.loads(x) for x in p.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, "b"])
        self.assertTrue(replies[0]["result"]["meta_seen"])
        got = {s["attributes"]["gen_ai.tool.name"]: s for s in S.read(out)}
        self.assertEqual((got["echo"]["status"], got["boom"]["status"]), ("ok", "error"))
        self.assertEqual(got["boom"]["attributes"]["error.type"], "server_error")
        self.assertEqual(got["echo"]["attributes"]["mcp.server.name"], "fake")


class Collector(http.server.BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        Collector.bodies.append((self.path, json.loads(self.rfile.read(int(self.headers["content-length"])))))
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

    def log_message(self, format, *args):  # quiet test output
        pass


class Otlp(unittest.TestCase):
    def test_partial_export_counts_accepted_without_replay(self):
        rows = [S.make("a" * 32, str(i), "mcp__srv__tool", int(NOW * 1e9), int(NOW * 1e9), source="tap")
                for i in range(5)]
        def response(body):
            r = mock.MagicMock()
            r.__enter__.return_value = r
            r.status = 200
            r.read.return_value = json.dumps(body).encode()
            return r
        with mock.patch("urllib.request.urlopen", side_effect=[response({}), response({"partialSuccess": {
                "rejectedSpans": "1", "errorMessage": "capacity"}})]) as send:
            with self.assertRaises(otlp.PartialExportError) as caught:
                otlp.send(rows, "http://collector.invalid", batch=2)
        self.assertEqual((caught.exception.accepted, caught.exception.rejected), (3, 1))
        self.assertEqual(caught.exception.collector_message, "capacity")
        self.assertEqual(send.call_count, 2)

    def test_export_shape(self):
        httpd = http.server.HTTPServer(("127.0.0.1", 0), Collector)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = int(NOW * 1e9)
        rows = [S.make("a" * 32, "c1", "mcp__srv__tool", base, base + 5_000_000, source="tap", args={"q": 1}),
                S.make("a" * 32, "c2", "mcp__srv__tool", base, None, source="tap")]
        self.assertEqual(otlp.send(rows, f"http://127.0.0.1:{httpd.server_port}"), 2)
        path, body = Collector.bodies[-1]
        self.assertEqual(path, "/v1/traces")
        span = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        keys = {a["key"]: a["value"] for a in span["attributes"]}
        self.assertEqual((span["kind"], keys["mcp.method.name"], keys["gen_ai.tool.name"]),
                         (3, {"stringValue": "tools/call"}, {"stringValue": "tool"}))
        self.assertEqual(keys["mcp.request.argument_keys"], {"arrayValue": {"values": [{"stringValue": "q"}]}})
        httpd.shutdown(); httpd.server_close()


if __name__ == "__main__":
    unittest.main()
