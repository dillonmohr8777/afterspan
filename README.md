# afterspan

OpenTelemetry traces for MCP, rebuilt from the agent transcripts you already have.

Your coding agents already write down every MCP call they make: Claude Code in `~/.claude/projects`, Codex in
`~/.codex/sessions`. afterspan reads those transcripts and turns each call into a span: which server, which tool,
how long the agent waited, whether it failed and why. You get a month of history on the first run, with no proxy,
no SDK change and nothing to install in your MCP servers.

It works with servers in any language, including third-party ones you can't instrument. A live stdio tap covers
servers you want to watch going forward, and an OTLP exporter sends everything to Jaeger, Grafana Tempo,
Honeycomb, Langfuse or any OpenTelemetry collector.

```bash
pip install git+https://github.com/dillonmohr8777/afterspan
afterspan import --days 30 --out spans.jsonl      # Claude Code + Codex transcripts -> spans
afterspan report spans.jsonl                      # per-server latency and errors
afterspan report spans.jsonl --by tool --html report.html
```

```
4210 calls in 312 sessions, 2026-10-05T09:02 to 2026-11-03T17:40 ({'claude-code': 3980, 'codex': 230})
name                calls   err%   p50 ms    p95 ms    max ms  errors
github               1204   1.1%      410     1,920    48,200  rate_limit 9, auth 4
web-search            980   4.3%      910     8,600    61,000  rate_limit 31, validation 11
playwright            655   8.1%      600    10,200   113,000  tool_error 35, server_error 12, timeout 6
```
(Illustrative numbers.)

The HTML report adds a "Fix first" list: the tools that fail most often, weighted by error rate, with their error
types (rate limit, auth, timeout, validation, server error, connection, not found).

## Live tap

Put the tap in front of any stdio MCP server. It forwards bytes unchanged and records a span per JSON-RPC request.

```json
{"command": "afterspan", "args": ["tap", "--name", "github", "--out", "/tmp/github-spans.jsonl", "--",
                                  "npx", "-y", "@modelcontextprotocol/server-github"]}
```

`--propagate` adds a W3C `traceparent` to each request's `params._meta` (MCP SEP-414), so a server that reads it can
join the same trace.

## Export

```bash
afterspan export spans.jsonl --endpoint http://localhost:4318
OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=..." afterspan export spans.jsonl --endpoint https://api.honeycomb.io
```

Spans use the OpenTelemetry MCP and GenAI semantic conventions (`mcp.method.name`, `gen_ai.tool.name`,
`gen_ai.operation.name=execute_tool`, `mcp.session.id`, `error.type`), so they sit next to traces from the official
MCP SDKs that emit OpenTelemetry natively.

## What a span keeps

Argument and result values never leave the transcript. A span stores the tool name, timings, status, a value-free
error type, byte sizes, and the top-level argument *names* (names that look like keys, tokens or passwords are
counted, not listed). Span files are safe to share.

## How durations are measured

From transcripts, duration is the result's timestamp minus the call's: what the agent waited, including transport
and queueing. Tools that wait on a person (approval prompts) show long tails, so read p50 and p95, not max.
App and harness tools that aren't MCP servers (Claude Code's `ccd_*` plan and session tools) are hidden by default;
`--harness` brings them back. Resumed sessions repeat earlier lines, so calls are de-duplicated by call id and cut
by their own start time.

## Status

0.1: Claude Code and Codex importers, stdio tap, report (text, JSON, HTML), OTLP/HTTP JSON export. Python 3.9+,
no dependencies. Next: Streamable HTTP tap, Cursor and Gemini CLI importers.

MIT licensed.
