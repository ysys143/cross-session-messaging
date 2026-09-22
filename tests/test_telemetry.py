"""Spans, counters and the promise that none of it can break a caller."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_xsm import TempState  # noqa: E402


class SpanTest(TempState):
    def test_ids_have_the_w3c_shape_and_do_not_repeat(self):
        from xsm import telemetry
        seen = set()
        for _ in range(50):
            with telemetry.span("x") as s:
                self.assertEqual(len(s.trace_id), 32)
                self.assertEqual(len(s.span_id), 16)
                int(s.trace_id, 16), int(s.span_id, 16)      # hex, not anything else
                seen.add((s.trace_id, s.span_id))
        self.assertEqual(len(seen), 50)

    def test_a_span_is_written_when_it_ends(self):
        from xsm import paths, telemetry
        with telemetry.span("xsm.send", {"xsm.msg.kind": "task"}, kind="PRODUCER") as s:
            s.set_attribute("xsm.result.status", "delivered")
            traceparent = s.traceparent()
        rows = paths.read_jsonl(telemetry.SPANS)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "xsm.send")
        self.assertEqual(row["kind"], "PRODUCER")
        self.assertEqual(row["status"], "OK")
        self.assertIsNone(row["parent_id"])
        self.assertEqual(row["attributes"],
                         {"xsm.msg.kind": "task", "xsm.result.status": "delivered"})
        self.assertGreaterEqual(row["duration_ms"], 0)
        self.assertEqual(traceparent, "00-%s-%s-01" % (row["trace_id"], row["span_id"]))

    def test_a_nested_span_hangs_off_the_one_already_running(self):
        from xsm import paths, telemetry
        with telemetry.span("outer") as outer:
            with telemetry.span("inner") as inner:
                self.assertEqual(inner.trace_id, outer.trace_id)
                self.assertEqual(inner.parent_id, outer.span_id)
            with telemetry.span("sibling") as sibling:
                self.assertEqual(sibling.parent_id, outer.span_id)
                self.assertNotEqual(sibling.span_id, inner.span_id)
        names = [r["name"] for r in paths.read_jsonl(telemetry.SPANS)]
        self.assertEqual(names, ["inner", "sibling", "outer"], "children close first")

    def test_a_traceparent_continues_the_senders_trace(self):
        from xsm import telemetry
        incoming = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        with telemetry.span("xsm.receive.gate", traceparent=incoming) as s:
            self.assertEqual(s.trace_id, "a" * 32)
            self.assertEqual(s.parent_id, "b" * 16)
            self.assertNotEqual(s.span_id, "b" * 16, "a new span, not the sender's")

    def test_a_broken_traceparent_starts_a_fresh_trace_instead_of_raising(self):
        from xsm import telemetry
        for bad in (None, "", "nonsense", "00-short-%s-01" % ("b" * 16),
                    "00-%s-%s-01" % ("0" * 32, "b" * 16),      # all-zero trace id
                    "00-%s-%s-01" % ("a" * 32, "0" * 16),      # all-zero span id
                    "00-%s-01" % ("a" * 32)):
            self.assertIsNone(telemetry.parse_traceparent(bad), bad)
            with telemetry.span("x", traceparent=bad) as s:
                self.assertEqual(len(s.trace_id), 32)
                self.assertIsNone(s.parent_id)

    def test_an_exception_is_tagged_and_still_raised(self):
        from xsm import paths, telemetry
        with self.assertRaises(ValueError):
            with telemetry.span("xsm.deliver"):
                raise ValueError("socket is gone")
        row = paths.read_jsonl(telemetry.SPANS)[0]
        self.assertEqual(row["status"], "ERROR")
        self.assertIn("socket is gone", row["message"])
        self.assertIn("ValueError", row["message"])


class MetricTest(TempState):
    def test_counters_and_histograms_land_in_their_own_log(self):
        from xsm import paths, telemetry
        telemetry.counter("xsm.send.count", 1, {"xsm.result.status": "delivered"})
        telemetry.counter("xsm.send.count", 1, {"xsm.result.status": "refused"})
        telemetry.histogram("xsm.deliver.duration", 0.25, {"xsm.transport": "uds"})
        rows = paths.read_jsonl(telemetry.METRICS)
        self.assertEqual([r["metric"] for r in rows], ["sum", "sum", "histogram"])
        self.assertEqual(rows[2]["value"], 250.0, "seconds in, milliseconds stored")
        self.assertEqual(rows[0]["attributes"], {"xsm.result.status": "delivered"})

    def test_summarize_counts_calls_errors_and_percentiles(self):
        from xsm import telemetry
        for _ in range(9):
            with telemetry.span("xsm.send"):
                pass
        with self.assertRaises(RuntimeError):
            with telemetry.span("xsm.send"):
                raise RuntimeError("no")
        telemetry.counter("xsm.send.count", 1)
        telemetry.counter("xsm.send.count", 2)
        report = telemetry.summarize()
        self.assertEqual(report["span_count"], 10)
        self.assertEqual(report["spans"]["xsm.send"]["count"], 10)
        self.assertEqual(report["spans"]["xsm.send"]["errors"], 1)
        self.assertGreaterEqual(report["spans"]["xsm.send"]["p95_ms"], 0)
        self.assertEqual(report["counters"]["xsm.send.count"], {"total": 3, "count": 2})

    def test_summarize_on_an_empty_home_says_nothing_rather_than_dividing_by_zero(self):
        from xsm import telemetry
        self.assertEqual(telemetry.summarize(),
                         {"span_count": 0, "spans": {}, "point_count": 0,
                          "counters": {}, "histograms": {}})


class NeverBreaksTheCallerTest(TempState):
    def test_the_kill_switch_yields_no_span_and_writes_nothing(self):
        os.environ["XSM_NO_TELEMETRY"] = "1"
        try:
            for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
                del sys.modules[mod]
            from xsm import paths, telemetry
            paths.HOME = self.tmp
            self.assertTrue(telemetry.DISABLED)
            with telemetry.span("xsm.send") as s:
                self.assertIsNone(s)
            telemetry.counter("xsm.send.count")
            telemetry.histogram("xsm.deliver.duration", 1.0)
            self.assertFalse(os.path.exists(paths.path(telemetry.SPANS)))
            self.assertFalse(os.path.exists(paths.path(telemetry.METRICS)))
        finally:
            os.environ.pop("XSM_NO_TELEMETRY", None)

    def test_a_span_that_cannot_start_yields_none_instead_of_raising(self):
        from xsm import telemetry
        with mock.patch("os.urandom", side_effect=OSError("no entropy")):
            with telemetry.span("xsm.send") as s:
                self.assertIsNone(s)

    def test_an_unserializable_attribute_is_dropped_not_raised(self):
        from xsm import paths, telemetry
        with telemetry.span("xsm.send") as s:
            s.set_attribute("bad", object())
        self.assertEqual(paths.read_jsonl(telemetry.SPANS), [], "nothing written, nothing raised")

    def test_a_body_inside_a_span_still_returns_its_value(self):
        from xsm import telemetry

        def work():
            with telemetry.span("x"):
                return "answer"
        self.assertEqual(work(), "answer")


if __name__ == "__main__":
    unittest.main()
