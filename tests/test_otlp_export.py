"""The OTLP payloads, and what happens when the collector is not there."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_telemetry import needs_telemetry  # noqa: E402
from tests.test_xsm import TempState  # noqa: E402


class Collector:
    """A stand-in for an OTLP collector that keeps what it was sent."""

    def __init__(self, status=200):
        self.received = []
        received, code = self.received, status

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, json.loads(body),
                                 self.headers.get("Content-Type")))
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@needs_telemetry
class PayloadTest(TempState):
    def test_a_span_becomes_an_otlp_span(self):
        from xsm import otlp_export, paths, telemetry
        with telemetry.span("xsm.send", {"xsm.msg.kind": "task", "retries": 2, "ok": True},
                            kind="PRODUCER") as parent:
            with telemetry.span("xsm.deliver"):
                pass
        payload = otlp_export.build_traces(paths.read_jsonl(telemetry.SPANS))
        scope = payload["resourceSpans"][0]["scopeSpans"][0]
        self.assertEqual(scope["scope"]["name"], "xsm")
        service = {a["key"]: a["value"]["stringValue"]
                   for a in payload["resourceSpans"][0]["resource"]["attributes"]}
        self.assertEqual(service["service.name"], "xsm")

        child, root = scope["spans"]                 # the inner one finished first
        self.assertEqual(root["name"], "xsm.send")
        self.assertEqual(root["kind"], 4, "PRODUCER")
        self.assertEqual(root["status"], {"code": 1}, "OK")
        self.assertNotIn("parentSpanId", root, "a trace root carries no parent")
        self.assertEqual(child["parentSpanId"], root["spanId"])
        self.assertEqual(child["traceId"], root["traceId"])
        self.assertEqual(len(root["traceId"]), 32)
        self.assertEqual(len(root["spanId"]), 16)

        attrs = {a["key"]: a["value"] for a in root["attributes"]}
        self.assertEqual(attrs["xsm.msg.kind"], {"stringValue": "task"})
        self.assertEqual(attrs["retries"], {"intValue": "2"}, "64-bit ints travel as strings")
        self.assertEqual(attrs["ok"], {"boolValue": True})
        for key in ("startTimeUnixNano", "endTimeUnixNano"):
            self.assertIsInstance(root[key], str)
            self.assertGreater(int(root[key]), 1_600_000_000_000_000_000)
        self.assertGreaterEqual(int(root["endTimeUnixNano"]), int(root["startTimeUnixNano"]))
        self.assertEqual(parent.trace_id, root["traceId"])

    def test_an_error_span_carries_its_message(self):
        from xsm import otlp_export, paths, telemetry
        with self.assertRaises(ValueError):
            with telemetry.span("xsm.deliver"):
                raise ValueError("socket is gone")
        span = otlp_export.build_traces(paths.read_jsonl(telemetry.SPANS)
                                        )["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        self.assertEqual(span["status"]["code"], 2, "ERROR")
        self.assertIn("socket is gone", span["status"]["message"])

    def test_counters_and_histograms_become_otlp_metrics(self):
        from xsm import otlp_export, paths, telemetry
        telemetry.counter("xsm.send.count", 1, {"xsm.result.status": "delivered"})
        telemetry.counter("xsm.send.count", 1, {"xsm.result.status": "delivered"})
        telemetry.counter("xsm.send.count", 1, {"xsm.result.status": "refused"})
        telemetry.histogram("xsm.deliver.duration", 0.007)      # 7ms -> second bucket
        telemetry.histogram("xsm.deliver.duration", 3.0)        # 3000ms
        metrics = {m["name"]: m for m in otlp_export.build_metrics(
            paths.read_jsonl(telemetry.METRICS))["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}

        total = metrics["xsm.send.count"]["sum"]
        self.assertTrue(total["isMonotonic"])
        self.assertEqual(total["aggregationTemporality"], 1, "delta: no resident process")
        by_status = {p["attributes"][0]["value"]["stringValue"]: p for p in total["dataPoints"]}
        self.assertEqual(by_status["delivered"]["asDouble"], 2)
        self.assertEqual(by_status["refused"]["asDouble"], 1)

        point = metrics["xsm.deliver.duration"]["histogram"]["dataPoints"][0]
        self.assertEqual(point["count"], "2")
        self.assertEqual(point["sum"], 3007.0)
        self.assertEqual(point["min"], 7.0)
        self.assertEqual(point["max"], 3000.0)
        self.assertEqual(len(point["bucketCounts"]), len(point["explicitBounds"]) + 1)
        self.assertEqual(sum(int(c) for c in point["bucketCounts"]), 2)
        self.assertEqual(point["bucketCounts"][1], "1", "7ms lands in the 5-10ms bucket")


@needs_telemetry
class ExportTest(TempState):
    def setUp(self):
        super().setUp()
        self.collector = Collector()
        self.addCleanup(self.collector.close)

    def _record(self):
        from xsm import telemetry
        with telemetry.span("xsm.send"):
            pass
        telemetry.counter("xsm.send.count", 1)

    def test_what_was_recorded_reaches_the_collector(self):
        from xsm import otlp_export
        self._record()
        result = otlp_export.export_once(self.collector.url)
        self.assertEqual((result["spans_sent"], result["points_sent"]), (1, 1))
        self.assertTrue(result["traces_ok"] and result["metrics_ok"])
        paths_seen = {path: body for path, body, _ in self.collector.received}
        self.assertEqual(set(paths_seen), {"/v1/traces", "/v1/metrics"})
        self.assertEqual({ct for _, _, ct in self.collector.received}, {"application/json"})
        self.assertEqual(paths_seen["/v1/traces"]["resourceSpans"][0]["scopeSpans"][0]
                         ["spans"][0]["name"], "xsm.send")

    def test_the_cursor_means_nothing_is_sent_twice(self):
        from xsm import otlp_export
        self._record()
        otlp_export.export_once(self.collector.url)
        again = otlp_export.export_once(self.collector.url)
        self.assertEqual((again["spans_sent"], again["points_sent"]), (0, 0))
        self.assertEqual(len(self.collector.received), 2, "nothing posted the second time")
        self._record()
        third = otlp_export.export_once(self.collector.url)
        self.assertEqual(third["spans_sent"], 1, "only what was added since")

    def test_a_collector_that_is_down_keeps_the_cursor_where_it_was(self):
        from xsm import otlp_export
        self._record()
        dead = "http://127.0.0.1:1"                  # nothing listens here
        result = otlp_export.export_once(dead)
        self.assertEqual((result["traces_ok"], result["spans_sent"]), (False, 0))
        retried = otlp_export.export_once(self.collector.url)
        self.assertEqual(retried["spans_sent"], 1, "the same line goes out again, not lost")

    def test_a_refusing_collector_is_not_treated_as_success(self):
        from xsm import otlp_export
        broken = Collector(status=500)
        self.addCleanup(broken.close)
        self._record()
        result = otlp_export.export_once(broken.url)
        self.assertFalse(result["traces_ok"])
        self.assertEqual(otlp_export.export_once(self.collector.url)["spans_sent"], 1)

    def test_an_empty_home_posts_nothing(self):
        from xsm import otlp_export
        result = otlp_export.export_once(self.collector.url)
        self.assertEqual((result["spans_sent"], result["points_sent"]), (0, 0))
        self.assertIsNone(result["traces_ok"])
        self.assertEqual(self.collector.received, [])


@needs_telemetry
class CommandTest(TempState):
    def _run(self, argv):
        import contextlib
        import io
        from xsm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_metrics_says_so_when_there_is_nothing(self):
        code, out = self._run(["metrics"])
        self.assertEqual(code, 0)
        self.assertIn("nothing recorded yet", out)

    def test_metrics_prints_and_serialises_what_was_recorded(self):
        from xsm import telemetry
        with telemetry.span("xsm.send"):
            pass
        telemetry.counter("xsm.send.count", 1, {"xsm.result.status": "delivered"})
        code, text = self._run(["metrics"])
        self.assertEqual(code, 0)
        self.assertIn("xsm.send", text)
        self.assertIn("1 call(s)", text)
        code, raw = self._run(["metrics", "--json"])
        report = json.loads(raw)
        self.assertEqual(report["span_count"], 1)
        self.assertEqual(report["counters"]["xsm.send.count"]["total"], 1)

    def test_otlp_export_reports_what_it_sent(self):
        from xsm import telemetry
        collector = Collector()
        self.addCleanup(collector.close)
        with telemetry.span("xsm.send"):
            pass
        code, text = self._run(["otlp-export", "--endpoint", collector.url])
        self.assertEqual(code, 0)
        self.assertIn("sent 1 span(s)", text)
        code, raw = self._run(["otlp-export", "--endpoint", collector.url, "--json"])
        self.assertEqual(json.loads(raw)["spans_sent"], 0, "the cursor already moved")

    def test_otlp_export_says_when_the_collector_is_unreachable(self):
        from xsm import telemetry
        with telemetry.span("xsm.send"):
            pass
        code, text = self._run(["otlp-export", "--endpoint", "http://127.0.0.1:1"])
        self.assertEqual(code, 0, "an unreachable collector is not a command failure")
        self.assertIn("could not reach the collector", text)


class EndpointTest(TempState):
    def test_the_standard_variable_is_honoured(self):
        from xsm import otlp_export
        self.assertEqual(otlp_export.endpoint(), "http://localhost:4318")
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4318/"
        self.addCleanup(os.environ.pop, "OTEL_EXPORTER_OTLP_ENDPOINT", None)
        self.assertEqual(otlp_export.endpoint(), "http://collector:4318")
        self.assertEqual(otlp_export.endpoint("http://other:4318"), "http://other:4318",
                         "an explicit endpoint wins")


if __name__ == "__main__":
    unittest.main()
