"""Send spans to any OpenTelemetry collector over OTLP/HTTP JSON (no SDK needed).

  afterspan export spans.jsonl --endpoint http://localhost:4318   (Jaeger, Grafana Tempo, Honeycomb, Langfuse, ...)
Headers for hosted backends come from OTEL_EXPORTER_OTLP_HEADERS ("key=value,key2=value2"), the standard variable.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

STATUS = {"ok": 1, "error": 2, "unfinished": 0}


class PartialExportError(RuntimeError):
    def __init__(self, accepted, rejected, message, unsent=0):
        self.accepted, self.rejected, self.collector_message, self.unsent = accepted, rejected, message, unsent
        super().__init__(f"collector accepted {accepted} spans, rejected {rejected}, unsent {unsent}: {message}")


def _val(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, list):
        return {"arrayValue": {"values": [_val(x) for x in v]}}
    return {"stringValue": str(v)}


def payload(spans, service="afterspan"):
    otel = []
    for s in spans:
        end = s["end_ns"] if s.get("end_ns") is not None else s["start_ns"]
        attrs = dict(s["attributes"], **{"afterspan.source": s["source"], "afterspan.status": s["status"]})
        otel.append({"traceId": s["trace_id"], "spanId": s["span_id"], "name": s["name"], "kind": 3,  # CLIENT
                     "startTimeUnixNano": str(s["start_ns"]), "endTimeUnixNano": str(end),
                     "attributes": [{"key": k, "value": _val(v)} for k, v in attrs.items() if v is not None],
                     "status": {"code": STATUS[s["status"]], **({"message": attrs.get("error.type", "")} if s["status"] == "error" else {})}})
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                               "scopeSpans": [{"scope": {"name": "afterspan", "version": "0.1.0"}, "spans": otel}]}]}


def send(spans, endpoint, service="afterspan", batch=500):
    url = endpoint.rstrip("/") + ("" if endpoint.rstrip("/").endswith("/v1/traces") else "/v1/traces")
    headers = {"content-type": "application/json"}
    for pair in filter(None, os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(",")):
        k, _, v = pair.partition("=")
        if k.strip():
            headers[urllib.parse.unquote(k.strip())] = urllib.parse.unquote(v.strip())
    sent, spans = 0, [s for s in spans if s.get("start_ns")]
    total = len(spans)
    for i in range(0, total, batch):
        chunk = spans[i:i + batch]
        req = urllib.request.Request(url, data=json.dumps(payload(chunk, service)).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status >= 300:
                    raise RuntimeError(f"collector answered HTTP {r.status}")
                response = json.loads(r.read() or b"{}")
        except (urllib.error.URLError, OSError, RuntimeError, ValueError) as exc:
            raise PartialExportError(sent, 0, str(exc), total - sent) from exc
        partial = response.get("partialSuccess") or {}
        try:
            rejected = int(partial.get("rejectedSpans", 0))
        except (TypeError, ValueError):
            rejected = -1
        if rejected < 0 or rejected > len(chunk):
            raise PartialExportError(sent, 0, "collector returned an invalid rejectedSpans count", total - sent)
        if rejected:
            accepted = sent + len(chunk) - rejected
            raise PartialExportError(accepted, rejected, partial.get("errorMessage", ""),
                                     total - sent - len(chunk))
        sent += len(chunk)
    return sent
