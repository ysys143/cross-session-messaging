#!/usr/bin/env python3
"""Measure what the telemetry in xsm costs, one piece at a time.

Instrumentation that a person can feel is instrumentation they will turn off,
so the question is not "is it fast" but "how much of a send is it". The three
pieces are measured separately because they fail differently: id generation is
a syscall, serialisation is CPU, and the append is disk, and only the last one
varies with the machine.

Measures the real functions, not copies of them — a benchmark of a
reimplementation tells you nothing about the code that ships.

Usage:
    tools/bench_telemetry.py [--json] [--iterations N]
"""
import argparse
import json
import os
import platform
import shutil
import sys
import tempfile
import timeit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SPAN = {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "00f067aa0ba902b7",
        "parent_id": None, "name": "xsm.send", "kind": "PRODUCER", "start": 1758540000.123,
        "duration_ms": 12.5, "status": "OK", "message": "",
        "attributes": {"xsm.msg.kind": "task", "xsm.target.ref": "9f8e7d",
                       "xsm.result.status": "sent-unconfirmed"}}


def _us(seconds: float) -> float:
    return seconds * 1_000_000


def bench_ids(n: int) -> float:
    from xsm import telemetry
    return _us(timeit.timeit(lambda: (telemetry._new_id(16), telemetry._new_id(8)),
                             number=n) / n)


def bench_serialise(n: int) -> float:
    return _us(timeit.timeit(lambda: json.dumps(SPAN, ensure_ascii=False), number=n) / n)


def bench_append(n: int) -> float:
    from xsm import paths
    return _us(timeit.timeit(lambda: paths.append_jsonl("bench.jsonl", SPAN), number=n) / n)


def bench_span(n: int) -> float:
    """A whole span as the code actually uses one: ids, the body, and the write."""
    from xsm import telemetry

    def one():
        with telemetry.span("xsm.send", {"xsm.msg.kind": "task"}, kind="PRODUCER") as span:
            span.set_attribute("xsm.result.status", "sent-unconfirmed")
    return _us(timeit.timeit(one, number=n) / n)


def measure(n: int) -> dict:
    home = tempfile.mkdtemp(prefix="xsm-bench-")
    os.environ["XSM_HOME"] = home
    os.environ.pop("XSM_NO_TELEMETRY", None)
    for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
        del sys.modules[mod]
    try:
        from xsm import paths
        paths.HOME = home
        paths.ensure_home()
        ids, serialise = bench_ids(n), bench_serialise(n)
        append, whole = bench_append(n), bench_span(n)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    # Two spans is what one send records: xsm.send and xsm.deliver.
    per_send = whole * 2
    return {"iterations": n, "platform": sys.platform, "python": platform.python_version(),
            "id_pair_us": round(ids, 2), "serialise_us": round(serialise, 2),
            "append_jsonl_us": round(append, 2), "one_span_us": round(whole, 2),
            "per_send_ms": round(per_send / 1000, 4),
            "pct_of_a_10ms_send": round(per_send / 10_000 * 100, 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = measure(args.iterations)
    if args.json:
        print(json.dumps(result, indent=1))
        return 0
    print("%s, python %s, %d iterations\n" % (result["platform"], result["python"],
                                              result["iterations"]))
    for label, key in (("id pair (16B + 8B)", "id_pair_us"),
                       ("json.dumps of a span", "serialise_us"),
                       ("append_jsonl", "append_jsonl_us"),
                       ("one whole span", "one_span_us")):
        print("  %-24s %8.2f us" % (label, result[key]))
    print("\n  %-24s %8.4f ms  (%.2f%% of a 10ms send)" % (
        "two spans, as one send", result["per_send_ms"], result["pct_of_a_10ms_send"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
