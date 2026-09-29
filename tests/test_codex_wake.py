"""Waking a Codex thread stopped with Esc through its app-server daemon.

Every daemon here is a fake: a Unix socket in a temporary folder, linked from
a temporary CODEX_HOME the way Codex links its real one. XSM_HOME is a
temporary folder too; no real Codex home, daemon or session is touched.
"""
import base64
import contextlib
import io
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BUSY = "thread already has an active or pending turn"
NOT_LOADED = "resume the thread before starting a queued message"


class FakeDaemon:
    """Speaks WebSocket + JSON-RPC like Codex's control socket, as `mode` says:
    started, busy, not-loaded, garbage (not WebSocket), silent (never replies),
    chatty (a ping and a notification before each reply)."""

    def __init__(self, codex_home, mode):
        self.mode, self.requests = mode, []
        self.dir = tempfile.mkdtemp(prefix="xd", dir="/tmp")    # AF_UNIX paths are short
        real = os.path.join(self.dir, "c.sock")
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(real)
        self.sock.listen(4)
        link = os.path.join(codex_home, "app-server-control", "app-server-control.sock")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(real, link)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def close(self):
        self.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                self._serve(conn)
            except (OSError, ValueError, struct.error):
                pass
            finally:
                conn.close()

    def _serve(self, conn):
        conn.settimeout(10)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        if self.mode == "garbage":
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nno")
            return
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: x\r\n\r\n")
        rest = buf.split(b"\r\n\r\n", 1)[1]
        while True:
            msg, rest = self._read(conn, rest)
            if msg is None:
                return
            self.requests.append(msg)
            if self.mode == "silent" or "id" not in msg:
                continue
            if self.mode == "chatty":
                conn.sendall(self._frame(b"hi", opcode=0x9))
                conn.sendall(self._frame(json.dumps({"method": "thread/status/changed",
                                                     "params": {}}).encode()))
            conn.sendall(self._frame(json.dumps(self._reply(msg)).encode()))

    def _reply(self, msg):
        rid = msg["id"]
        if msg["method"] == "initialize":
            return {"id": rid, "result": {"userAgent": "fake"}}
        if msg["method"] == "thread/loaded/list":
            # One page per list in self.loaded; the cursor is the next index.
            pages = getattr(self, "loaded", None) or [[]]
            i = int((msg.get("params") or {}).get("cursor") or 0)
            return {"id": rid, "result": {"data": pages[i],
                                          "nextCursor": str(i + 1) if i + 1 < len(pages) else None}}
        if msg["method"] != "thread/queue/start":
            return {"id": rid, "error": {"code": -32601, "message": "not here"}}
        if self.mode == "busy":
            return {"id": rid, "error": {"code": -32600, "message": BUSY}}
        if self.mode == "not-loaded":
            return {"id": rid, "error": {"code": -32600, "message": NOT_LOADED}}
        return {"id": rid, "result": {"turn": {"id": "turn-1", "status": "inProgress",
                                                "items": []}}}

    @staticmethod
    def _frame(data, opcode=0x1):
        n = len(data)
        head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else
                                         bytes([126]) + struct.pack(">H", n))
        return head + data

    @staticmethod
    def _read(conn, buf):
        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = conn.recv(65536)
                if not chunk:
                    raise OSError("closed")
                buf += chunk
        try:
            while True:
                need(2)
                b0, b1 = buf[0], buf[1]
                n, off = b1 & 0x7F, 2
                if n == 126:
                    need(4)
                    n, off = struct.unpack(">H", buf[2:4])[0], 4
                assert b1 & 0x80, "a client frame is masked"
                need(off + 4 + n)
                key = buf[off:off + 4]
                data = bytes(b ^ key[i % 4] for i, b in enumerate(buf[off + 4:off + 4 + n]))
                buf = buf[off + 4 + n:]
                if b0 & 0x0F == 0xA:          # our ping's pong
                    continue
                return json.loads(data), buf
        except OSError:
            return None, buf


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xsm-wake-")
        os.environ["XSM_HOME"] = os.path.join(self.tmp, "xsm")
        # Who `me` is comes from these; the suite may run inside a real session.
        for key in ("XSM_NO_CODEX_WAKE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                    "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
                    "XSM_SANDBOXED"):
            self.addCleanup(lambda k=key, v=os.environ.get(key):
                            os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None))
            os.environ.pop(key, None)
        for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
            del sys.modules[mod]
        from xsm import paths
        paths.HOME = os.environ["XSM_HOME"]
        paths.ensure_home()
        self.home = os.path.join(self.tmp, "codex")
        os.makedirs(self.home)
        self.daemons = []

    def tearDown(self):
        for d in self.daemons:
            d.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def daemon(self, mode):
        d = FakeDaemon(self.home, mode)
        self.daemons.append(d)
        return d


class StartQueuedTest(Base):
    def start(self, timeout=3.0):
        from xsm import codex_daemon
        return codex_daemon.start_queued(self.home, "T1", "Q1", timeout=timeout)

    def test_a_started_turn_is_started(self):
        d = self.daemon("started")
        self.assertEqual(self.start()[0], "started")
        methods = [r.get("method") for r in d.requests]
        self.assertEqual(methods, ["initialize", "initialized", "thread/queue/start"])
        self.assertEqual(d.requests[0]["params"]["capabilities"], {"experimentalApi": True})
        self.assertEqual(d.requests[2]["params"], {"threadId": "T1", "queuedSubmissionId": "Q1"})

    def test_pings_and_notifications_are_passed_over(self):
        self.daemon("chatty")
        self.assertEqual(self.start()[0], "started")

    def test_a_busy_thread_is_busy(self):
        self.daemon("busy")
        outcome, detail = self.start()
        self.assertEqual(outcome, "busy")
        self.assertIn("active or pending turn", detail)

    def test_a_thread_the_daemon_does_not_hold_is_not_loaded(self):
        self.daemon("not-loaded")
        self.assertEqual(self.start()[0], "not-loaded")

    def test_no_socket_is_unavailable(self):
        self.assertEqual(self.start()[0], "unavailable")

    def test_a_socket_nobody_listens_on_is_unavailable(self):
        d = self.daemon("started")
        d.sock.close()
        self.assertEqual(self.start()[0], "unavailable")

    def test_something_that_is_not_websocket_is_unavailable(self):
        self.daemon("garbage")
        self.assertEqual(self.start()[0], "unavailable")

    def test_a_silent_daemon_times_out_as_unavailable(self):
        self.daemon("silent")
        began = time.monotonic()
        self.assertEqual(self.start(timeout=0.5), ("unavailable", "timed out"))
        self.assertLess(time.monotonic() - began, 2.0)

    def test_it_never_resumes_or_starts_a_turn(self):
        for mode in ("started", "busy", "not-loaded"):
            for d in self.daemons:
                d.close()
            shutil.rmtree(os.path.join(self.home, "app-server-control"), ignore_errors=True)
            d = self.daemon(mode)
            self.start()
            asked = {r.get("method") for r in d.requests}
            self.assertFalse(asked & {"thread/resume", "turn/start"}, mode)


class AdapterWakeTest(Base):
    def _fake_codex(self, output):
        path = os.path.join(self.tmp, "codex-bin")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\necho '%s'\nexit 0\n" % output)
        os.chmod(path, 0o755)
        return path

    def test_the_queued_id_codex_printed_is_started(self):
        from xsm import adapters
        d = self.daemon("started")
        codex = self._fake_codex("Queued message `019f-q` for thread `T1`")
        with mock.patch.object(adapters, "codex_bins", lambda: [codex]):
            out = adapters.to_codex(self.home, "T1", "hello")
        self.assertEqual(out.wake, "started")
        self.assertEqual(d.requests[-1]["params"], {"threadId": "T1",
                                                     "queuedSubmissionId": "019f-q"})

    def test_the_id_codex_0158_really_prints_is_found(self):
        # No backticks in the real output (measured 2026-09-28).
        from xsm import adapters
        d = self.daemon("started")
        codex = self._fake_codex("Queued message 01a0e7c4-fe85-7d23 for thread T1.")
        with mock.patch.object(adapters, "codex_bins", lambda: [codex]):
            self.assertEqual(adapters.to_codex(self.home, "T1", "hello").wake, "started")
        self.assertEqual(d.requests[-1]["params"]["queuedSubmissionId"], "01a0e7c4-fe85-7d23")

    def test_a_busy_thread_keeps_its_item_queued(self):
        from xsm import adapters
        self.daemon("busy")
        codex = self._fake_codex("Queued message `q2` for thread `T1`")
        with mock.patch.object(adapters, "codex_bins", lambda: [codex]):
            self.assertEqual(adapters.to_codex(self.home, "T1", "hello").wake, "busy")

    def test_a_row_written_directly_is_started_by_its_own_id(self):
        import sqlite3
        from xsm import adapters
        con = sqlite3.connect(os.path.join(self.home, "queue_1.sqlite"))
        con.execute("create table queued_items (id text, thread_id text, payload_json text, "
                    "queue_order integer, created_at_ms integer, updated_at_ms integer)")
        con.commit()
        con.close()
        d = self.daemon("started")
        failed = mock.Mock(returncode=1, stdout="", stderr="no rollout found for thread id T1")
        with mock.patch.object(adapters.subprocess, "run", lambda *a, **k: failed), \
                mock.patch.object(adapters, "codex_bins", lambda: ["/bin/codex"]):
            out = adapters.to_codex(self.home, "T1", "hello")
        con = sqlite3.connect(os.path.join(self.home, "queue_1.sqlite"))
        (row_id,) = con.execute("select id from queued_items").fetchone()
        con.close()
        self.assertEqual(out.wake, "started")
        self.assertEqual(d.requests[-1]["params"]["queuedSubmissionId"], row_id)

    def test_opting_out_asks_nothing(self):
        from xsm import adapters, config
        d = self.daemon("started")
        codex = self._fake_codex("Queued message `q3` for thread `T1`")
        with mock.patch.object(adapters, "codex_bins", lambda: [codex]), \
                mock.patch.dict(os.environ, {"XSM_NO_CODEX_WAKE": "1"}):
            self.assertEqual(adapters.to_codex(self.home, "T1", "hello").wake, "skipped")
        config._save({"codex_wake": False})
        with mock.patch.object(adapters, "codex_bins", lambda: [codex]):
            self.assertEqual(adapters.to_codex(self.home, "T1", "hello").wake, "skipped")
        self.assertEqual(d.requests, [])

    def test_the_sender_is_told_it_started(self):
        from xsm import adapters, registry, send
        registry._running_codex = lambda: []
        cwd = os.path.join(self.tmp, "proj")
        os.makedirs(cwd)
        registry.upsert("codex", self.home, "me", os.getpid(), cwd, name="me")
        registry.upsert("codex", self.home, "peer", os.getpid(), cwd, name="peer")
        me = registry.by_session("codex", "me")
        started = adapters.Queued("Queued message `q` for thread `peer`")
        started.wake = "started"
        busy = adapters.Queued("Queued message `q` for thread `peer`")
        busy.wake = "busy"
        with mock.patch.dict(os.environ, {"CODEX_SANDBOX": "", "XSM_SANDBOXED": ""}):
            with mock.patch.object(adapters, "to_codex", lambda *a: started):
                r = send.send("codex:peer", "hi", sender=me)
            self.assertEqual(r.status, "sent-unconfirmed", "still waiting for the receipt")
            self.assertIn("started now", r.reason)
            with mock.patch.object(adapters, "to_codex", lambda *a: busy):
                r = send.send("codex:peer", "hi", sender=me)
            self.assertNotIn("started now", r.reason)
            self.assertIn("xsm inbox", r.reason)


class InterruptedListTest(Base):
    def _rollout(self, tid, events):
        day = os.path.join(self.home, "sessions", "2026", "09", "28")
        os.makedirs(day, exist_ok=True)
        path = os.path.join(day, "rollout-2026-09-28T10-00-00-%s.jsonl" % tid)
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {"id": tid}}) + "\n")
            for kind, extra in events:
                fh.write(json.dumps({"type": "event_msg",
                                     "payload": dict({"type": kind, "turn_id": "u"}, **extra)})
                         + "\n")
        return path

    def test_the_last_turn_decides(self):
        from xsm import registry
        self._rollout("t-esc", [("task_started", {}), ("turn_aborted", {"reason": "interrupted"})])
        self._rollout("t-again", [("turn_aborted", {"reason": "interrupted"}),
                                  ("task_started", {})])
        self._rollout("t-done", [("task_started", {}), ("task_complete", {})])
        self._rollout("t-other", [("turn_aborted", {"reason": "replaced"})])
        self.assertTrue(registry.codex_interrupted(self.home, "t-esc"))
        self.assertFalse(registry.codex_interrupted(self.home, "t-again"))
        self.assertFalse(registry.codex_interrupted(self.home, "t-done"))
        self.assertFalse(registry.codex_interrupted(self.home, "t-other"))
        self.assertFalse(registry.codex_interrupted(self.home, "t-none"))

    def test_list_marks_an_interrupted_codex(self):
        from xsm import cli, registry
        registry._running_codex = lambda: []
        cwd = os.path.join(self.tmp, "proj")
        os.makedirs(cwd)
        registry.upsert("codex", self.home, "t-esc", os.getpid(), cwd, name="stopped")
        registry.upsert("codex", self.home, "t-run", os.getpid(), cwd, name="running")
        self._rollout("t-esc", [("task_started", {}), ("turn_aborted", {"reason": "interrupted"})])
        self._rollout("t-run", [("task_started", {})])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--table", "--dir", cwd])
        rows = {line.split("|")[2].strip(): line for line in out.getvalue().splitlines()[2:]}
        self.assertIn("interrupted (Esc)", rows["stopped"])
        self.assertNotIn("interrupted", rows["running"])


class HostedThreadLivenessTest(Base):
    """Issue #5: a thread hosted by the app-server daemon runs its hooks under
    the daemon, so every thread of a home records the daemon's pid. Whether
    the thread is still open is the daemon's `thread/loaded/list`."""

    DEAD_PID = 999999

    def loaded(self, *pages):
        d = self.daemon("started")
        d.loaded = [list(p) for p in pages]
        return d

    def record(self, thread, pid, **extra):
        rec = {"runtime": "codex", "home": self.home, "session_id": thread, "pid": pid,
               "app_server": True}
        rec.update(extra)
        return rec

    def test_the_list_is_read_across_pages(self):
        from xsm import codex_daemon
        d = self.loaded(["T1", "T2"], ["T3"])
        self.assertEqual(codex_daemon.loaded_threads(self.home), {"T1", "T2", "T3"})
        methods = [r.get("method") for r in d.requests]
        self.assertEqual(methods, ["initialize", "initialized", "thread/loaded/list",
                                   "thread/loaded/list"])
        self.assertEqual(d.requests[3]["params"], {"cursor": "1"})

    def test_one_home_is_asked_once_for_a_whole_list(self):
        from xsm import codex_daemon
        d = self.loaded(["T1"])
        for _ in range(5):
            codex_daemon.loaded_threads(self.home)
        self.assertEqual([r.get("method") for r in d.requests].count("initialize"), 1)

    def test_no_answer_is_none_not_an_empty_list(self):
        from xsm import codex_daemon
        self.assertIsNone(codex_daemon.loaded_threads(self.home))       # no socket
        codex_daemon._LOADED_CACHE.clear()
        self.daemon("garbage")
        self.assertIsNone(codex_daemon.loaded_threads(self.home))

    def test_a_silent_daemon_is_none_within_the_timeout(self):
        from xsm import codex_daemon
        self.daemon("silent")
        began = time.monotonic()
        self.assertIsNone(codex_daemon.loaded_threads(self.home, timeout=0.5))
        self.assertLess(time.monotonic() - began, 2.0)

    def test_a_thread_the_daemon_let_go_is_ended_though_the_daemon_lives(self):
        from xsm import identity
        self.loaded(["other"])
        self.assertEqual(identity.state_reason(self.record("T1", os.getpid())),
                         ("ended", "thread_unloaded"))

    def test_a_loaded_thread_is_live_though_the_daemon_it_registered_under_is_gone(self):
        """Measured 2026-09-29: the daemon updated itself to 0.159 under a
        running TUI, which reconnected to the new one; the pid on record died."""
        from xsm import identity
        self.loaded(["T1"])
        self.assertEqual(identity.state_reason(self.record("T1", self.DEAD_PID)),
                         ("live", "thread_loaded"))

    def test_no_answer_falls_back_to_the_pid_and_ignores_the_beacon(self):
        """Every hosted thread's MCP server is a child of the daemon, so the
        beacon on record may be another thread's; a dead one says nothing."""
        from xsm import identity
        with mock.patch.object(identity, "lstart", lambda pid: None):
            rec = self.record("T1", os.getpid(), mcp_pid=self.DEAD_PID)
            self.assertEqual(identity.state_of(rec), "live")
            self.assertEqual(identity.state_of(self.record("T1", self.DEAD_PID)), "stale")

    def test_a_thread_under_its_own_tui_still_uses_the_pid(self):
        from xsm import identity
        self.loaded([])
        rec = self.record("T1", os.getpid())
        rec.pop("app_server")
        with mock.patch.object(identity, "lstart", lambda pid: None):
            self.assertEqual(identity.state_of(rec), "live")

    def test_a_legacy_record_under_the_daemon_pid_is_judged_by_the_daemon(self):
        """Records written before `app_server` existed have no such key. The
        beacon rule read them ended while a TUI held the thread (2026-09-29)."""
        from xsm import identity
        self.loaded(["T1"])
        rec = self.record("T1", os.getpid(), mcp_pid=self.DEAD_PID)
        rec.pop("app_server")
        with mock.patch.object(identity, "is_app_server", lambda pid: True):
            self.assertEqual(identity.state_reason(rec), ("live", "thread_loaded"))
        with mock.patch.object(identity, "is_app_server", lambda pid: False), \
                mock.patch.object(identity, "lstart", lambda pid: None):
            self.assertEqual(identity.state_reason(rec), ("ended", "thread_replaced"),
                             "a TUI pid keeps the beacon rule")

    def test_an_explicit_false_flag_never_probes_the_process_table(self):
        from xsm import identity
        rec = self.record("T1", os.getpid(), app_server=False)
        with mock.patch.object(identity, "is_app_server", side_effect=AssertionError("probed")), \
                mock.patch.object(identity, "lstart", lambda pid: None):
            self.assertEqual(identity.state_of(rec), "live")

    def test_list_shows_the_unloaded_thread_as_ended(self):
        from xsm import registry
        registry._running_codex = lambda: []
        self.loaded(["T-open"])
        with mock.patch.object(registry.identity, "is_app_server", lambda pid: True):
            registry.upsert("codex", self.home, "T-open", os.getpid(), self.tmp, name="open")
            registry.upsert("codex", self.home, "T-quit", os.getpid(), self.tmp, name="quit")
        by = {r["session_id"]: r for r in registry.records()}
        self.assertEqual(by["T-open"]["state"], "live")
        self.assertEqual(by["T-quit"]["state"], "ended")
        self.assertEqual(by["T-quit"]["end_reason"], "thread_unloaded")


class DeadHomesLatencyTest(Base):
    """Review 2026-09-29: the receive hook asks every home's daemon on each
    prompt under a 10 s limit. Homes whose daemon accepts and never answers
    must cost a bounded, mostly one-time amount."""

    HOMES = 5

    def dead_homes(self):
        homes = []
        for i in range(self.HOMES):
            home = os.path.join(self.tmp, "dead%d" % i)
            os.makedirs(home)
            d = FakeDaemon(home, "silent")
            self.daemons.append(d)
            homes.append(home)
        return homes

    def test_hook_budget_bounds_the_cost_of_several_hung_homes(self):
        from xsm import codex_daemon
        homes = self.dead_homes()
        codex_daemon.use_hook_budget()
        began = time.monotonic()
        for home in homes:
            self.assertIsNone(codex_daemon.loaded_threads(home))
        first = time.monotonic() - began
        began = time.monotonic()
        for _ in range(20):
            for home in homes:
                self.assertIsNone(codex_daemon.loaded_threads(home))
        again = time.monotonic() - began
        print("\n[latency] %d hung homes: first pass %.2fs, 100 repeat asks %.4fs"
              % (self.HOMES, first, again))
        self.assertLess(first, self.HOMES * 0.2 + 0.6)
        self.assertLess(again, 0.2, "a failure is remembered, not asked again")

    def test_a_failure_is_remembered_longer_than_an_answer(self):
        from xsm import codex_daemon
        self.assertGreater(codex_daemon._LOADED_FAIL_TTL, 10 * codex_daemon._LOADED_TTL)
        (home,) = self.dead_homes()[:1]
        self.assertIsNone(codex_daemon.loaded_threads(home, timeout=0.1))
        key = os.path.realpath(home)
        stamped, value = codex_daemon._LOADED_CACHE[key]
        self.assertIsNone(value)
        self.assertGreaterEqual(stamped, time.monotonic() - 0.5,
                                "stamped when the query ended, not when it began")

    def test_the_receive_hook_takes_the_short_budget(self):
        from xsm import codex_daemon, receive
        self.assertEqual(codex_daemon.DEFAULT_TIMEOUT, 1.0)
        with mock.patch.object(sys, "stdin", io.StringIO("{}")), \
                mock.patch.object(receive, "handle", lambda data: None), \
                mock.patch.object(receive, "_emit", lambda *a: None):
            receive.main()
        self.assertEqual(codex_daemon.DEFAULT_TIMEOUT, codex_daemon.HOOK_TIMEOUT)

    def test_records_over_hung_homes_stay_within_the_hook_budget(self):
        from xsm import codex_daemon, identity, registry
        registry._running_codex = lambda: []
        homes = self.dead_homes()
        with mock.patch.object(identity, "is_app_server", lambda pid: True):
            for i, home in enumerate(homes):
                registry.upsert("codex", home, "T%d" % i, os.getpid(), self.tmp, name="t%d" % i)
            codex_daemon.use_hook_budget()
            began = time.monotonic()
            rows = registry.records()
        took = time.monotonic() - began
        print("\n[latency] records() over %d hung homes: %.2fs" % (self.HOMES, took))
        self.assertEqual(len(rows), self.HOMES)
        self.assertLess(took, self.HOMES * 0.2 + 0.8)


if __name__ == "__main__":
    unittest.main()
