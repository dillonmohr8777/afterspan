"""afterspan command line.

  afterspan import [--claude-code DIR] [--codex DIR] [--days 30] [--all-tools] --out spans.jsonl
  afterspan report spans.jsonl [--by server|tool] [--json] [--html report.html]
  afterspan tap --name NAME --out spans.jsonl [--propagate] -- COMMAND ...
  afterspan export spans.jsonl --endpoint http://localhost:4318 [--service NAME]
"""
import argparse
import json
import os
import sys

from . import __version__, importers, otlp, report, spans, tap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--" in argv and argv[:1] == ["tap"]:
        i = argv.index("--"); argv, cmd = argv[:i], argv[i + 1:]
    else:
        cmd = []
    ap = argparse.ArgumentParser(prog="afterspan", description="OpenTelemetry traces for MCP, rebuilt from agent transcripts.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import", help="rebuild spans from Claude Code and Codex transcripts")
    p.add_argument("--claude-code", default="~/.claude/projects", help="Claude Code projects dir ('' to skip)")
    p.add_argument("--codex", default="~/.codex/sessions", help="Codex sessions dir ('' to skip)")
    p.add_argument("--days", type=float, default=30)
    p.add_argument("--all-tools", action="store_true", help="include built-in tools (Bash, WebSearch, ...), not only MCP")
    p.add_argument("--harness", action="store_true", help="include app/harness tools (ccd_*: plans, wrap-ups), hidden by default")
    p.add_argument("--out", required=True)
    p = sub.add_parser("report", help="per-server or per-tool latency and error table")
    p.add_argument("file"); p.add_argument("--by", choices=["server", "tool"], default="server")
    p.add_argument("--json", action="store_true"); p.add_argument("--html")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--alias", action="append", default=[], metavar="ID=NAME",
                   help="rename a server in the report, e.g. a claude.ai connector UUID (repeatable)")
    p = sub.add_parser("tap", help="run an MCP stdio server behind a recording tap")
    p.add_argument("--name", required=True); p.add_argument("--out", required=True)
    p.add_argument("--propagate", action="store_true", help="add a W3C traceparent to params._meta")
    p = sub.add_parser("export", help="send spans to an OTLP/HTTP collector")
    p.add_argument("file"); p.add_argument("--endpoint", default=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"))
    p.add_argument("--service", default="afterspan")
    a = ap.parse_args(argv)

    if a.cmd == "import":
        found = []
        if a.claude_code and os.path.isdir(os.path.expanduser(a.claude_code)):
            found += importers.claude_code(a.claude_code, a.days, mcp_only=not a.all_tools, harness=a.harness)
        if a.codex and os.path.isdir(os.path.expanduser(a.codex)):
            found += importers.codex(a.codex, a.days, mcp_only=not a.all_tools, harness=a.harness)
        if os.path.exists(a.out):
            os.remove(a.out)
        print(json.dumps({"spans": spans.write(found, a.out), "out": a.out}))
    elif a.cmd == "report":
        rows = list(spans.read(a.file))
        al = dict(x.split("=", 1) for x in a.alias if "=" in x)
        s = report.summarize(rows, a.by, al)
        if a.html:
            with open(a.html, "w") as f:
                f.write(report.html_page(report.summarize(rows, "server", al), report.summarize(rows, "tool", al)))
        print(json.dumps(s, indent=1) if a.json else report.text(s, a.limit))
    elif a.cmd == "tap":
        if not cmd:
            ap.error("tap needs a server command after --")
        return tap.run(cmd, a.name, a.out, a.propagate)
    elif a.cmd == "export":
        try:
            print(json.dumps({"sent": otlp.send(list(spans.read(a.file)), a.endpoint, a.service)}))
        except otlp.PartialExportError as exc:
            print(json.dumps({"sent": exc.accepted, "rejected": exc.rejected, "error": exc.collector_message}), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
