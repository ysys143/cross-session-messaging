"""Spans, counters and histograms, written as JSONL under XSM_HOME.

No OpenTelemetry SDK: this repo installs nothing beyond the standard library,
so the ids and the wire format are produced here instead. They are the real
ones — trace ids are 16 bytes and span ids 8, as W3C Trace Context requires,
and `xsm otlp-export` turns these lines into OTLP payloads any collector
accepts. What is not reused is the SDK, not the standard.

Telemetry must never change what xsm does. A span that cannot start yields
None and the caller proceeds untouched; a span that cannot be written is a
missed observation, not a failed send. That is the same contract
`paths.append_jsonl` already keeps for the audit log, for the same reason: a
hook that dies takes the gate with it.
"""
from __future__ import annotations

import contextvars
import os
import time
from contextlib import contextmanager

from . import paths

SPANS = "otel-spans.jsonl"
METRICS = "otel-metrics.jsonl"

# Read once: a lookup per span would cost more than the flag saves.
DISABLED = bool(os.environ.get("XSM_NO_TELEMETRY"))

_current: contextvars.ContextVar = contextvars.ContextVar("xsm-span", default=None)


class Span:
    def __init__(self, name: str, trace_id: str, span_id: str, parent_id: str | None,
                 kind: str, attributes: dict | None):
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_id = parent_id
        self.kind = kind
        self.attributes = dict(attributes or {})
        self.start = time.time()
        self.status = "OK"
        self.message = ""

    def set_attribute(self, key: str, value) -> None:
        self.attributes[key] = value

    def set_status(self, status: str, message: str = "") -> None:
        self.status = status
        self.message = message[:200]

    def traceparent(self) -> str:
        """This span as a W3C traceparent, for whatever carries it across a
        process boundary: the message envelope, or an SSH request."""
        return "00-%s-%s-01" % (self.trace_id, self.span_id)


@contextmanager
def span(name: str, attributes: dict | None = None, *, kind: str = "INTERNAL",
         traceparent: str | None = None):
    """Time one step. Nests under the span already running in this process
    (contextvars, so no argument has to be threaded through), or continues a
    trace that arrived from somewhere else when `traceparent` is given.

    Yields None when telemetry is off or could not start, so every caller
    guards with `if span is not None` and behaves identically either way.
    """
    if DISABLED:
        yield None
        return
    try:
        trace_id, span_id, parent_id = _ids(traceparent)
        current = Span(name, trace_id, span_id, parent_id, kind, attributes)
        token = _current.set(current)
    except (OSError, TypeError, ValueError):
        yield None
        return
    try:
        yield current
    except Exception as err:
        # Tagged, then re-raised untouched: this decides nothing about control
        # flow, it only records what the caller was already going to do.
        current.set_status("ERROR", "%s: %s" % (type(err).__name__, err))
        raise
    finally:
        _current.reset(token)
        _record(current)


def annotate(key: str, value) -> None:
    """Set an attribute on whatever span is open in this process, if any.

    For code deep in a call that has no span of its own to hand — how
    registry.me() identified the caller, say — so the fact lands on the
    command's span without a span argument threaded through every signature.
    """
    current = _current.get()
    if current is not None:
        current.attributes[key] = value


def bump(key: str, by: int = 1) -> None:
    """Add to a counter attribute on the open span: for things that happen
    many times per command, like one liveness verdict per session listed."""
    current = _current.get()
    if current is not None:
        value = current.attributes.get(key)
        current.attributes[key] = (value if isinstance(value, int) else 0) + by


def _ids(traceparent: str | None) -> tuple:
    """(trace_id, span_id, parent_id) for a span about to start."""
    resumed = parse_traceparent(traceparent)
    if resumed:
        return resumed[0], _new_id(8), resumed[1]
    parent = _current.get()
    if parent is not None:
        return parent.trace_id, _new_id(8), parent.span_id
    return _new_id(16), _new_id(8), None


def _new_id(nbytes: int) -> str:
    return os.urandom(nbytes).hex()


def parse_traceparent(value: str | None) -> tuple | None:
    """(trace_id, span_id) from a W3C traceparent, or None when it is not one.

    Anything arriving from another machine can be truncated or hand-edited, and
    a malformed header must start a new trace rather than raise on the way in.
    """
    if not value:
        return None
    parts = value.split("-")
    if len(parts) != 4 or len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    if parts[1] == "0" * 32 or parts[2] == "0" * 16:     # W3C: all-zero is invalid
        return None
    return parts[1], parts[2]


def _record(s: Span) -> None:
    try:
        paths.append_jsonl(SPANS, {
            "trace_id": s.trace_id, "span_id": s.span_id, "parent_id": s.parent_id,
            "name": s.name, "kind": s.kind, "start": s.start,
            "duration_ms": (time.time() - s.start) * 1000,
            "status": s.status, "message": s.message, "attributes": s.attributes,
        })
    except (OSError, TypeError, ValueError):
        pass


def counter(name: str, value: float = 1, attributes: dict | None = None) -> None:
    _point("sum", name, value, attributes)


def histogram(name: str, seconds: float, attributes: dict | None = None) -> None:
    """Record a duration. Seconds in, milliseconds stored — every other
    duration in these logs is in milliseconds, and mixing the two silently is
    the kind of thing nobody notices until a dashboard is wrong."""
    _point("histogram", name, seconds * 1000, attributes)


def _point(kind: str, name: str, value: float, attributes: dict | None) -> None:
    if DISABLED:
        return
    try:
        paths.append_jsonl(METRICS, {"metric": kind, "name": name, "value": value,
                                     "attributes": attributes or {}})
    except (OSError, TypeError, ValueError):
        pass


def summarize(limit: int = 5000) -> dict:
    """What `xsm metrics` prints. Sorting the whole window for a percentile is
    fine at this size — these logs hold thousands of lines, not millions."""
    spans, points = paths.read_jsonl(SPANS, limit=limit), paths.read_jsonl(METRICS, limit=limit)
    by_span: dict = {}
    for row in spans:
        agg = by_span.setdefault(row.get("name", "?"), {"values": [], "errors": 0})
        agg["values"].append(row.get("duration_ms", 0))
        if row.get("status") == "ERROR":
            agg["errors"] += 1
    counters, histograms = {}, {}
    for row in points:
        into = counters if row.get("metric") == "sum" else histograms
        into.setdefault(row.get("name", "?"), []).append(row.get("value", 0))
    return {
        "span_count": len(spans),
        "spans": {name: _stats(agg["values"], agg["errors"]) for name, agg in by_span.items()},
        "point_count": len(points),
        "counters": {name: {"total": sum(v), "count": len(v)} for name, v in counters.items()},
        "histograms": {name: _stats(v, 0) for name, v in histograms.items()},
    }


def _stats(values: list, errors: int) -> dict:
    ordered = sorted(values)
    count = len(ordered)
    return {"count": count, "errors": errors,
            "avg_ms": sum(ordered) / count if count else 0.0,
            "p95_ms": ordered[int(count * 0.95)] if count else 0.0}
