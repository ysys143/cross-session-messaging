"""Spans, counters and the promise that none of it can break a caller."""
import contextlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_xsm import TempState  # noqa: E402

# Read at import, before any test can change it: these assert on recorded
# output, which the kill switch is supposed to remove.
needs_telemetry = unittest.skipIf(os.environ.get("XSM_NO_TELEMETRY"),
                                  "XSM_NO_TELEMETRY is set; there is nothing to record")


@contextlib.contextmanager
def kill_switch(value):
    """Set XSM_NO_TELEMETRY and put back whatever was there, so a run under the
    switch does not lose it partway through the suite."""
    before = os.environ.get("XSM_NO_TELEMETRY")
    os.environ["XSM_NO_TELEMETRY"] = value
    for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
        del sys.modules[mod]
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("XSM_NO_TELEMETRY", None)
        else:
            os.environ["XSM_NO_TELEMETRY"] = before


@contextlib.contextmanager
def opened(*args, **kwargs):
    """telemetry.span for tests that run with telemetry on. The real one may
    yield None by design, and a test that reads the span should fail here,
    saying why, rather than later on an attribute of None."""
    from xsm import telemetry
    with telemetry.span(*args, **kwargs) as span:
        assert span is not None, "telemetry is off or could not start a span"
        yield span


@needs_telemetry
class SpanTest(TempState):
    def test_ids_have_the_w3c_shape_and_do_not_repeat(self):
        seen = set()
        for _ in range(50):
            with opened("x") as s:
                self.assertRegex(s.trace_id, r"^[0-9a-f]{32}$")
                self.assertRegex(s.span_id, r"^[0-9a-f]{16}$")
                seen.add((s.trace_id, s.span_id))
        self.assertEqual(len(seen), 50)

    def test_a_span_is_written_when_it_ends(self):
        from xsm import paths, telemetry
        with opened("xsm.send", {"xsm.msg.kind": "task"}, kind="PRODUCER") as s:
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
        with opened("outer") as outer:
            with opened("inner") as inner:
                self.assertEqual(inner.trace_id, outer.trace_id)
                self.assertEqual(inner.parent_id, outer.span_id)
            with opened("sibling") as sibling:
                self.assertEqual(sibling.parent_id, outer.span_id)
                self.assertNotEqual(sibling.span_id, inner.span_id)
        names = [r["name"] for r in paths.read_jsonl(telemetry.SPANS)]
        self.assertEqual(names, ["inner", "sibling", "outer"], "children close first")

    def test_a_traceparent_continues_the_senders_trace(self):
        incoming = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        with opened("xsm.receive.gate", traceparent=incoming) as s:
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
            with opened("x", traceparent=bad) as s:
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


@needs_telemetry
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
        with kill_switch("1"):
            from xsm import paths, telemetry
            paths.HOME = self.tmp
            self.assertTrue(telemetry.DISABLED)
            with telemetry.span("xsm.send") as s:
                self.assertIsNone(s)
            telemetry.counter("xsm.send.count")
            telemetry.histogram("xsm.deliver.duration", 1.0)
            self.assertFalse(os.path.exists(paths.path(telemetry.SPANS)))
            self.assertFalse(os.path.exists(paths.path(telemetry.METRICS)))

    @needs_telemetry
    def test_an_unserializable_attribute_is_dropped_not_raised(self):
        from xsm import paths, telemetry
        with opened("xsm.send") as s:
            s.set_attribute("bad", object())
        self.assertEqual(paths.read_jsonl(telemetry.SPANS), [], "nothing written, nothing raised")

    def test_a_span_that_cannot_start_yields_none_instead_of_raising(self):
        from xsm import telemetry
        with mock.patch("os.urandom", side_effect=OSError("no entropy")):
            with telemetry.span("xsm.send") as s:
                self.assertIsNone(s)

    def test_a_body_inside_a_span_still_returns_its_value(self):
        from xsm import telemetry

        def work():
            with telemetry.span("x"):
                return "answer"
        self.assertEqual(work(), "answer")


@needs_telemetry
class SendInstrumentationTest(TempState):
    """What a send actually records, end to end."""

    def _pair(self):
        from xsm import registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        sender = registry.upsert("codex", home, "s-1", os.getpid(), self.tmp, name="sender")
        registry.upsert("codex", home, "t-1", os.getpid(), self.tmp, name="target")
        return sender

    def _deliver(self, sender):
        """Send to a live target with the queue call stubbed out, returning
        (result, envelope-that-would-have-been-delivered)."""
        from xsm import send as send_mod
        seen = []
        # The inner one: patching to_codex itself would take the instrumentation
        # wrapper with it and there would be nothing left to observe.
        with mock.patch("xsm.adapters._to_codex", side_effect=lambda h, t, c: seen.append(c)):
            result = send_mod.send("target", "hello", sender=sender)
        return result, (seen[0] if seen else "")

    def test_the_envelope_carries_the_sending_spans_traceparent(self):
        from xsm import envelope, paths, telemetry
        result, wire = self._deliver(self._pair())
        self.assertEqual(result.status, "sent-unconfirmed")
        tp = envelope.parse(wire).header.get("traceparent")
        self.assertIsNotNone(tp, "the receiver has nothing to continue without it")
        row = [r for r in paths.read_jsonl(telemetry.SPANS) if r["name"] == "xsm.send"][0]
        self.assertEqual(tp, "00-%s-%s-01" % (row["trace_id"], row["span_id"]))
        self.assertEqual(row["attributes"]["xsm.result.status"], "sent-unconfirmed")
        self.assertEqual(row["attributes"]["xsm.msg.id"], result.msg_id)

    def test_delivery_is_timed_under_the_send_that_asked_for_it(self):
        from xsm import paths, telemetry
        self._deliver(self._pair())
        spans = {r["name"]: r for r in paths.read_jsonl(telemetry.SPANS)}
        self.assertEqual(spans["xsm.deliver"]["trace_id"], spans["xsm.send"]["trace_id"])
        self.assertEqual(spans["xsm.deliver"]["parent_id"], spans["xsm.send"]["span_id"])
        durations = [r for r in paths.read_jsonl(telemetry.METRICS)
                     if r["name"] == "xsm.deliver.duration"]
        self.assertEqual(durations[0]["attributes"]["xsm.delivery.outcome"], "ok")

    def test_a_refusal_is_counted_and_marked_without_being_changed(self):
        from xsm import paths, send as send_mod, telemetry
        result = send_mod.send("nobody-is-called-this", "hello", sender=self._pair())
        self.assertEqual(result.status, "refused")
        row = [r for r in paths.read_jsonl(telemetry.SPANS) if r["name"] == "xsm.send"][0]
        self.assertEqual(row["status"], "ERROR")
        self.assertEqual(row["attributes"]["xsm.result.status"], "refused")
        point = [r for r in paths.read_jsonl(telemetry.METRICS) if r["name"] == "xsm.send.count"][0]
        self.assertEqual(point["attributes"], {"xsm.result.status": "refused"})

    def test_a_failed_delivery_records_the_reason_and_still_raises_it(self):
        from xsm import adapters, paths, send as send_mod, telemetry
        sender = self._pair()
        blocked = adapters.DeliveryError("sandbox-blocked", "read-only database")
        with mock.patch("xsm.adapters._to_codex", side_effect=blocked):
            result = send_mod.send("target", "hello", sender=sender)
        self.assertEqual(result.status, "error")
        self.assertIn("sandbox-blocked", result.reason)
        deliver = [r for r in paths.read_jsonl(telemetry.SPANS) if r["name"] == "xsm.deliver"][0]
        self.assertEqual(deliver["status"], "ERROR")
        point = [r for r in paths.read_jsonl(telemetry.METRICS)
                 if r["name"] == "xsm.deliver.duration"][0]
        self.assertEqual(point["attributes"]["xsm.delivery.outcome"], "sandbox-blocked")


@needs_telemetry
class ReceiveInstrumentationTest(TempState):
    def _gate(self, prompt):
        from xsm import receive
        receive.register = lambda data, runtime: None        # identity unknown: blocks
        return receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "r1",
                               "cwd": self.tmp, "prompt": prompt, "session_title": "recv"})

    def test_the_receiver_joins_the_trace_the_sender_started(self):
        from xsm import envelope, paths, telemetry
        sender = {"name": "send", "alias": "claude-3", "ref": "aaaaaa", "session_id": "s1"}
        with opened("xsm.send") as sending:
            wire = envelope.build("hi", msg_id="m1", sender=sender, scope="dir:x",
                                  traceparent=sending.traceparent())
            sent = (sending.trace_id, sending.span_id)
        self._gate(wire)
        gate = [r for r in paths.read_jsonl(telemetry.SPANS) if r["name"] == "xsm.receive.gate"][0]
        self.assertEqual(gate["trace_id"], sent[0], "one trace across both processes")
        self.assertEqual(gate["parent_id"], sent[1])
        self.assertEqual(gate["kind"], "CONSUMER")
        self.assertEqual(gate["attributes"]["xsm.msg.id"], "m1")

    def test_a_blocked_message_is_marked_and_counted(self):
        from xsm import envelope, paths, telemetry
        sender = {"name": "send", "alias": "claude-3", "ref": "aaaaaa", "session_id": "s1"}
        out = self._gate(envelope.build("hi", msg_id="m1", sender=sender, scope="dir:x"))
        assert out is not None, "a peer message always gets a decision"
        self.assertEqual(out["decision"], "block", "unchanged: still refused")
        gate = [r for r in paths.read_jsonl(telemetry.SPANS) if r["name"] == "xsm.receive.gate"][0]
        self.assertEqual(gate["status"], "ERROR")
        self.assertEqual(gate["attributes"]["xsm.receive.decision"], "held")
        self.assertIn("cannot identify this session", gate["message"])
        point = [r for r in paths.read_jsonl(telemetry.METRICS)
                 if r["name"] == "xsm.receive.count"][0]
        self.assertEqual(point["attributes"], {"xsm.receive.decision": "held"})

    def test_a_human_prompt_opens_no_gate(self):
        from xsm import paths, telemetry
        self.assertIsNone(self._gate("my own prompt"))
        names = [r["name"] for r in paths.read_jsonl(telemetry.SPANS)]
        self.assertEqual(names, ["xsm.hook.UserPromptSubmit"],
                         "the hook ran and is on record; the gate only opens for peer messages")


@needs_telemetry
class OverheadTest(TempState):
    """A guard, not a measurement. docs/references/telemetry-overhead.md holds
    the real numbers (0.175ms per send, measured); this only catches something
    going catastrophically wrong, because a microbenchmark with a tight bound
    turns flaky the moment the machine is busy."""

    def test_a_span_costs_nowhere_near_a_millisecond(self):
        import time
        start = time.time()
        for _ in range(100):
            with opened("xsm.send", {"xsm.msg.kind": "task"}) as span:
                span.set_attribute("xsm.result.status", "sent-unconfirmed")
        each_ms = (time.time() - start) * 1000 / 100
        self.assertLess(each_ms, 10, "a span should cost microseconds; 10ms means something "
                                     "is writing far more than one line (measured: ~0.09ms)")


@needs_telemetry
class WorkerInstrumentationTest(TempState):
    def test_stopping_a_worker_records_its_lifetime(self):
        import time
        from xsm import paths, telemetry, workers
        workers.save({"name": "w1", "runtime": "codex", "mode": "headless",
                      "created": time.time() - 42})
        stopped = workers.stop("w1", reason="done")
        self.assertEqual(stopped["stopped"], "done", "unchanged: still the same record")
        points = {r["name"]: r for r in paths.read_jsonl(telemetry.METRICS)}
        self.assertEqual(points["xsm.worker.stopped"]["attributes"],
                         {"xsm.worker.runtime": "codex", "xsm.stop.reason": "done"})
        self.assertGreaterEqual(points["xsm.worker.lifetime"]["value"], 42_000)

    def test_a_worker_that_never_existed_still_raises(self):
        from xsm import workers
        with self.assertRaises(workers.WorkerError):
            workers.stop("no-such-worker")


class SendIsUnchangedByTelemetryTest(TempState):
    """The claim the whole design rests on, checked field by field."""

    def _result_fields(self):
        from xsm import registry, send as send_mod
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        sender = registry.upsert("codex", home, "s-1", os.getpid(), self.tmp, name="sender")
        registry.upsert("codex", home, "t-1", os.getpid(), self.tmp, name="target")
        seen = []
        with mock.patch("xsm.adapters._to_codex", side_effect=lambda h, t, c: seen.append(c)):
            ok = send_mod.send("target", "hello", sender=sender)
        refused = send_mod.send("nobody-is-called-this", "hello", sender=sender)
        body = seen[0].split("\n", 2)[2] if seen else ""
        return [(r.status, r.reason, bool(r.target)) for r in (ok, refused)] + [body]

    def test_the_same_results_with_telemetry_missing(self):
        baseline = self._result_fields()
        self.setUp()
        # A None entry in sys.modules is what makes an import raise ImportError.
        with mock.patch.dict(sys.modules, {"xsm.telemetry": None}):
            self.assertEqual(self._result_fields(), baseline)

    def test_the_same_results_when_telemetry_itself_fails(self):
        baseline = self._result_fields()
        self.setUp()
        # Only telemetry's own ids: os.urandom outright would also break the
        # uuid4 behind envelope.new_id, which is not what this is testing.
        with mock.patch("xsm.telemetry._new_id", side_effect=OSError("no entropy")):
            self.assertEqual(self._result_fields(), baseline)

    def test_the_same_results_with_the_kill_switch_on(self):
        baseline = self._result_fields()
        self.setUp()
        with kill_switch("1"):
            from xsm import paths
            paths.HOME = self.tmp
            self.assertEqual(self._result_fields(), baseline)


if __name__ == "__main__":
    unittest.main()
