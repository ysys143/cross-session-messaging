"""Sending what telemetry recorded to an OTLP collector, over HTTP/JSON.

OTLP has two encodings. The protobuf one needs a code generator and a
runtime; the JSON one is a document shape, and a document shape is something
the standard library can build. So this posts JSON to /v1/traces and
/v1/metrics at OTEL_EXPORTER_OTLP_ENDPOINT — the same variable and the same
paths an SDK would use — and any collector, Jaeger or Tempo takes it.

Nothing here runs on the send path. A person (or a cron line) runs
`xsm otlp-export`, it reads the JSONL from where it was appended and moves a
cursor. Export that fails leaves the cursor where it was, so the next run
sends the same lines again rather than losing them. That is also why this is
not a daemon: xsm starts no long-running process of its own (ADR-0003).
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request

from . import paths, telemetry

CURSOR = "otlp-cursor.json"
DEFAULT_ENDPOINT = "http://localhost:4318"

# OTLP enum values, which the JSON encoding spells as numbers. Trace and span
# ids go out as hex, which is the one place OTLP/JSON deliberately departs from
# the Protobuf JSON mapping's base64 for bytes — see opentelemetry-proto's
# docs/specification.md.
SPAN_KINDS = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5}
STATUS = {"UNSET": 0, "OK": 1, "ERROR": 2}
BUCKETS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]    # milliseconds


def endpoint(explicit: str | None = None) -> str:
    return (explicit or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or DEFAULT_ENDPOINT).rstrip("/")


def _attributes(mapping: dict) -> list:
    """OTLP wants every attribute tagged with its own type."""
    out = []
    for key, value in sorted((mapping or {}).items()):
        if isinstance(value, bool):
            typed = {"boolValue": value}
        elif isinstance(value, int):
            typed = {"intValue": str(value)}
        elif isinstance(value, float):
            typed = {"doubleValue": value}
        elif value is None:
            continue                       # an absent attribute, not a null one
        else:
            typed = {"stringValue": str(value)}
        out.append({"key": key, "value": typed})
    return out


def _resource() -> dict:
    from . import __version__
    return {"attributes": _attributes({"service.name": "xsm", "service.version": __version__,
                                       "host.name": socket.gethostname()})}


def _nanos(seconds: float) -> str:
    """64-bit ints travel as strings in OTLP/JSON; a float here would lose the
    low digits of a nanosecond timestamp."""
    return str(int(seconds * 1_000_000_000))


def build_traces(spans: list) -> dict:
    out = []
    for row in spans:
        start = row.get("start", 0)
        span = {
            "traceId": row.get("trace_id", ""), "spanId": row.get("span_id", ""),
            "name": row.get("name", ""),
            "kind": SPAN_KINDS.get(row.get("kind", "INTERNAL"), 1),
            "startTimeUnixNano": _nanos(start),
            "endTimeUnixNano": _nanos(start + row.get("duration_ms", 0) / 1000),
            "attributes": _attributes(row.get("attributes") or {}),
            "status": {"code": STATUS.get(row.get("status", "UNSET"), 0)},
        }
        if row.get("parent_id"):
            span["parentSpanId"] = row["parent_id"]      # omitted entirely at a trace root
        if row.get("message"):
            span["status"]["message"] = row["message"]
        out.append(span)
    return {"resourceSpans": [{"resource": _resource(),
                               "scopeSpans": [{"scope": {"name": "xsm"}, "spans": out}]}]}


def _series(points: list) -> dict:
    """One data point per (name, attribute set): OTLP keeps the dimensions in
    the attributes, not in the metric name."""
    grouped: dict = {}
    for row in points:
        key = (row.get("metric"), row.get("name"),
               json.dumps(row.get("attributes") or {}, sort_keys=True))
        grouped.setdefault(key, []).append(row.get("value", 0))
    return grouped


def build_metrics(points: list) -> dict:
    now = time.time()
    # One metric per name, with an attribute set per data point inside it —
    # emitting the same name twice would be two metrics that happen to collide.
    by_metric: dict = {}
    for (kind, name, attrs_json), values in _series(points).items():
        point = {"attributes": _attributes(json.loads(attrs_json)),
                 "startTimeUnixNano": _nanos(now), "timeUnixNano": _nanos(now)}
        if kind == "sum":
            point["asDouble"] = sum(values)
        else:
            counts = [0] * (len(BUCKETS) + 1)
            for value in values:
                index = next((i for i, edge in enumerate(BUCKETS) if value <= edge), len(BUCKETS))
                counts[index] += 1
            point.update({"count": str(len(values)), "sum": sum(values),
                          "min": min(values), "max": max(values),
                          "bucketCounts": [str(c) for c in counts],
                          "explicitBounds": [float(b) for b in BUCKETS]})
        by_metric.setdefault((kind, name), []).append(point)
    metrics = []
    for (kind, name), data_points in by_metric.items():
        if kind == "sum":
            # Delta, because each export covers only the lines since the last
            # one: there is no resident process holding a running total.
            metrics.append({"name": name, "unit": "1",
                            "sum": {"aggregationTemporality": 1, "isMonotonic": True,
                                    "dataPoints": data_points}})
        else:
            metrics.append({"name": name, "unit": "ms",
                            "histogram": {"aggregationTemporality": 1,
                                          "dataPoints": data_points}})
    return {"resourceMetrics": [{"resource": _resource(),
                                 "scopeMetrics": [{"scope": {"name": "xsm"},
                                                   "metrics": metrics}]}]}


def post(url: str, payload: dict) -> bool:
    """True when the collector took it. Never raises: a collector that is down
    is a reason to keep the cursor where it is, not to fail the command."""
    try:
        body = json.dumps(payload).encode()
    except (TypeError, ValueError):
        return False
    request = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _cursor() -> dict:
    return paths.read_json(paths.path(CURSOR), {}) or {}


def export_once(base_url: str) -> dict:
    """Send everything appended since the last successful export."""
    seen = _cursor()
    spans = paths.read_jsonl(telemetry.SPANS)[seen.get("spans", 0):]
    points = paths.read_jsonl(telemetry.METRICS)[seen.get("points", 0):]
    result = {"spans_sent": 0, "points_sent": 0, "traces_ok": None, "metrics_ok": None,
              "endpoint": base_url}
    moved = dict(seen)
    if spans:
        result["traces_ok"] = post(base_url + "/v1/traces", build_traces(spans))
        if result["traces_ok"]:
            result["spans_sent"] = len(spans)
            moved["spans"] = seen.get("spans", 0) + len(spans)
    if points:
        result["metrics_ok"] = post(base_url + "/v1/metrics", build_metrics(points))
        if result["metrics_ok"]:
            result["points_sent"] = len(points)
            moved["points"] = seen.get("points", 0) + len(points)
    if moved != seen:
        paths.write_json(paths.path(CURSOR), moved)
    return result


def run(base_url: str, *, interval: float = 5.0, rounds: int | None = None) -> int:
    """Export every `interval` seconds until interrupted. A person runs this in
    a terminal; nothing installs it as a service."""
    count = 0
    while rounds is None or count < rounds:
        export_once(base_url)
        count += 1
        if rounds is not None and count >= rounds:
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            break
    return 0
