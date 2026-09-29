"""Two machines on one box (ADR-0007): each has its own XSM_HOME and
authorized_keys, and a fake ssh plays sshd with forced commands."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKESSH = os.path.join(REPO, "tests", "fixtures", "fakessh.py")


class Inbox:
    """A listening inbox socket that keeps every frame it receives."""

    def __init__(self, path):
        self.path, self.frames = path, []
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(path)
        self.sock.listen(8)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b"\n"):
                    break
            conn.close()
            self.frames.append(data.decode())


class TwoMachinesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xr")
        self.socks = tempfile.mkdtemp(prefix="xs", dir="/tmp")
        self.m = {}
        for name in ("hostA", "hostB"):
            home = os.path.join(self.tmp, name)
            os.makedirs(os.path.join(home, "state"))
            os.makedirs(os.path.join(home, "proj"))
            os.makedirs(os.path.join(home, "claude", "sessions"))
            self.m[name] = {"home": os.path.join(home, "state"), "ak": os.path.join(home, "ak"),
                            "name": name, "proj": os.path.join(home, "proj"),
                            "claude": os.path.join(home, "claude")}
            open(self.m[name]["ak"], "w").close()
        self.inboxes = {n: Inbox(os.path.join(self.socks, "%s.sock" % n)) for n in self.m}
        for name in self.m:
            self.py(name, """
from xsm import config, registry, paths
import os
config.join("demo", %r)
registry.upsert("claude", %r, "s-%s", os.getpid() if False else %d, %r, name="agent", permission_mode="auto")
paths.write_json(os.path.join(%r, "sessions", "%d.json"), {"name": "agent", "sessionId": "s-%s",
                 "messagingSocketPath": %r})
""" % (self.m[name]["proj"], self.m[name]["claude"], name, os.getpid(), self.m[name]["proj"],
       self.m[name]["claude"], os.getpid(), name, self.inboxes[name].path))

    def tearDown(self):
        for i in self.inboxes.values():
            i.sock.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.socks, ignore_errors=True)

    def env(self, name, **extra):
        m = self.m[name]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "ORCA_"))}
        env.update({"XSM_HOME": m["home"], "XSM_AUTHORIZED_KEYS": m["ak"],
                    "XSM_HOSTNAME": name, "XSM_SSH": FAKESSH, "PYTHONPATH": REPO,
                    "FAKE_MACHINES": json.dumps({n: {"home": v["home"], "ak": v["ak"], "name": n}
                                                 for n, v in self.m.items()})})
        env.update(extra)
        return env

    def py(self, name, code, **extra):
        out = subprocess.run([sys.executable, "-c", code], env=self.env(name, **extra),
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def send(self, name, target, text, **kw):
        return json.loads(self.py(name, """
import json
from xsm import send, registry
r = send.send(%r, %r, sender=registry.by_session("claude", "s-%s"), **%r)
print(json.dumps({"status": r.status, "reason": r.reason, "id": r.msg_id}))
""" % (target, text, name, kw)))

    def gate(self, name, prompt):
        m = self.m[name]
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s-%s" % name,
                   "cwd": m["proj"], "prompt": prompt, "prompt_id": "p", "session_title": "agent"}
        out = subprocess.run([sys.executable, os.path.join(REPO, "hooks", "xsm-hook.py")],
                             input=json.dumps(payload), capture_output=True, text=True,
                             env=self.env(name, CLAUDE_CONFIG_DIR=m["claude"],
                                          CLAUDE_CODE_SESSION_ID="s-%s" % name,
                                          CLAUDE_CODE_MESSAGING_SOCKET=self.inboxes[name].path))
        return json.loads(out.stdout) if out.stdout.strip() else None

    def test_pair_send_gate_and_reply_across_machines(self):
        paired = self.py("hostA", """
from xsm import remote
print(remote.add("hostB", "demo", here=%r)["peer"])""" % self.m["hostA"]["proj"])
        self.assertEqual(paired, "hostB")
        # Both sides trust the other's xsm key, for the forced command only.
        for name in self.m:
            with open(self.m[name]["ak"]) as fh:
                line = fh.read()
            self.assertIn('command="', line)
            self.assertIn("no-pty", line)

        sent = self.send("hostA", "agent@claude@hostB", "hello from A", kind="task")
        self.assertIn(sent["status"], ("sent-unconfirmed", "delivered"), sent)
        frame = self.inboxes["hostB"].frames[-1]
        self.assertIn("origin=hostA", frame)
        self.assertNotIn("uds:", frame, "no socket reply address across machines")
        prompt = json.loads(frame)["message"]["content"]

        allowed = self.gate("hostB", prompt)
        context = allowed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("@hostA", context, "the reply command points back at the peer")

        forged = prompt.replace(sent["id"], "0000forged0000")
        self.assertEqual(self.gate("hostB", forged)["decision"], "block",
                         "an id this machine's receiver never recorded is refused")

        a_ref = self.py("hostA", 'from xsm import registry; print(registry.by_session("claude", "s-hostA")["ref"])')
        back = self.send("hostB", "ref:%s@hostA" % a_ref, "reply from B", kind="reply",
                         reply_to=sent["id"])
        self.assertIn(back["status"], ("sent-unconfirmed", "delivered"), back)
        self.assertIn("origin=hostB", self.inboxes["hostA"].frames[-1])

    def spans(self, name):
        """Every span that machine recorded, in the order it finished them."""
        path = os.path.join(self.m[name]["home"], "otel-spans.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    @unittest.skipIf(os.environ.get("XSM_NO_TELEMETRY"), "telemetry is switched off")
    def test_one_trace_spans_both_machines(self):
        """Three hops, three processes, two XSM_HOMEs: still one trace."""
        self.py("hostA", 'from xsm import remote; remote.add("hostB", "demo", here=%r)'
                % self.m["hostA"]["proj"])
        sent = self.send("hostA", "agent@claude@hostB", "hello from A", kind="task")
        self.assertIn(sent["status"], ("sent-unconfirmed", "delivered"), sent)
        self.gate("hostB", json.loads(self.inboxes["hostB"].frames[-1])["message"]["content"])

        a = {s["name"]: s for s in self.spans("hostA")}
        b = {s["name"]: s for s in self.spans("hostB")}
        self.assertIn("xsm.send", a)
        self.assertIn("xsm.remote.ssh", a)
        self.assertIn("xsm.remote.serve", b)
        self.assertIn("xsm.receive.gate", b)
        trace = a["xsm.send"]["trace_id"]
        for where, span in (("hostA ssh", a["xsm.remote.ssh"]), ("hostB serve", b["xsm.remote.serve"]),
                            ("hostB gate", b["xsm.receive.gate"])):
            self.assertEqual(span["trace_id"], trace, where)
        self.assertEqual(a["xsm.remote.ssh"]["parent_id"], a["xsm.send"]["span_id"])
        self.assertEqual(b["xsm.remote.serve"]["parent_id"], a["xsm.remote.ssh"]["span_id"],
                         "the far side hangs off the ssh call that reached it")
        self.assertEqual(b["xsm.receive.gate"]["parent_id"], b["xsm.remote.serve"]["span_id"])
        self.assertEqual(b["xsm.receive.gate"]["attributes"]["xsm.receive.decision"], "pass")

    def test_unpaired_and_wrong_project_are_refused(self):
        refused = self.send("hostA", "agent@claude@hostB", "x")
        self.assertNotIn(refused["status"], ("sent-unconfirmed", "delivered"),
                         "not paired: the @hostB is not a remote, so nothing goes out")
        self.py("hostA", 'from xsm import remote; remote.add("hostB", "demo", here=%r)' % self.m["hostA"]["proj"])
        self.py("hostB", 'from xsm import config; config.leave("demo", %r)' % self.m["hostB"]["proj"])
        out = self.send("hostA", "agent@claude@hostB", "x")
        self.assertEqual(out["status"], "refused")
        self.assertIn("not in project demo", out["reason"])


class IngressStateTest(unittest.TestCase):
    """The receiving side alone, in process: a message from a paired peer to a
    local session that has stopped is refused, as a local send would be. It
    used to be queued for nobody and reported to the peer as queued
    (2026-09-28)."""

    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.mkdtemp(prefix="xsm-ingress-")
        self.env = mock.patch.dict(os.environ, {"XSM_HOME": self.tmp})
        self.env.start()
        sys.path.insert(0, REPO)
        for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
            del sys.modules[mod]
        from xsm import config, paths, registry
        paths.HOME = self.tmp
        paths.ensure_home()
        registry._running_codex = lambda: []
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        config.join("demo", self.proj)
        raw = config._raw()
        raw["remotes"] = [{"peer": "hostA", "host": "hostA", "local_project": "demo",
                           "remote_project": "demo", "added": 0}]
        config._save(raw)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _codex(self, sid, pid):
        from xsm import registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, sid, pid, self.proj, name=sid)
        return registry.by_session("codex", sid)

    def _dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def _request(self, target):
        return {"op": "send", "project": "demo", "target": target, "id": "m-%s" % target,
                "body": "hi", "kind": "note",
                "sender": {"name": "agent", "alias": "claude", "ref": "aaaaaa"}}

    def test_ended_and_stale_targets_are_refused_without_delivery(self):
        from unittest import mock
        from xsm import adapters, ledger, registry, remote
        self._codex("gone", self._dead_pid())
        registry.mark_ended("codex", "gone", "prompt_input_exit")
        self._codex("crashed", self._dead_pid())

        def called(*args, **kw):
            raise AssertionError("an adapter was called for a stopped target")
        with mock.patch.multiple(adapters, to_claude=called, to_codex=called):
            for sid, state in (("gone", "ended"), ("crashed", "stale")):
                reply = remote._serve("hostA", self._request("codex:%s" % sid))
                self.assertEqual(reply["status"], "refused", reply)
                self.assertIn("is not running (%s)" % state, reply["error"])
        self.assertEqual(ledger.recent(), [], "nothing was recorded as queued")

    def test_a_live_target_is_still_delivered(self):
        from unittest import mock
        from xsm import adapters, remote
        self._codex("alive", os.getpid())
        sent = []
        with mock.patch.object(adapters, "to_codex", lambda *a: sent.append(a[1])):
            reply = remote._serve("hostA", self._request("codex:alive"))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(sent, ["alive"])


class LostAnswerTest(unittest.TestCase):
    """Issue #4: two XSM_HOMEs in one process, ssh replaced by a function that
    runs the peer's receiver under the other home. A send whose request never
    left must read `error`; one whose answer was lost must read `unknown`, not
    `queued`; and `xsm status` must be able to ask the peer what happened."""

    def setUp(self):
        from unittest import mock
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="xsm-lost-"))
        self.env = mock.patch.dict(os.environ, {"XSM_HOME": os.path.join(self.tmp, "hostA")})
        self.env.start()
        os.environ.pop("XSM_SSH", None)
        sys.path.insert(0, REPO)
        for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
            del sys.modules[mod]
        from xsm import config, paths, registry
        registry._running_codex = lambda: []
        self.homes, self.projs = {}, {}
        for me, peers in (("hostA", ["hostB"]), ("hostB", ["hostA", "hostC"])):
            self.homes[me] = os.path.join(self.tmp, me)
            self.projs[me] = os.path.join(self.homes[me], "proj")
            os.makedirs(self.projs[me])
            with self.on(me):
                paths.ensure_home()
                config.join("demo", self.projs[me])
                raw = config._raw()
                raw["remotes"] = [{"peer": p, "host": p, "local_project": "demo",
                                   "remote_project": "demo", "added": 0} for p in peers]
                config._save(raw)
        with self.on("hostB"):
            home = os.path.join(self.tmp, "codexhome")
            os.makedirs(home)
            registry.upsert("codex", home, "alive", os.getpid(), self.projs["hostB"], name="alive")
        self.delivered = []
        from xsm import adapters
        self.adapter = mock.patch.object(adapters, "to_codex",
                                         lambda home, sid, content: self.delivered.append(sid))
        self.adapter.start()
        self.sender = {"name": "agent", "alias": "claude", "ref": "aaaaaa",
                       "cwd": self.projs["hostA"]}

    def tearDown(self):
        self.adapter.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def on(self, host):
        """Run the block as that machine: its paths, config, ledger and registry."""
        from contextlib import contextmanager
        from xsm import paths

        @contextmanager
        def switched():
            before = paths.HOME
            paths.HOME = self.homes[host]
            try:
                yield
            finally:
                paths.HOME = before
        return switched()

    def ssh(self, how, then=None):
        """A stand-in for subprocess.run. `how`: refused (ssh cannot connect),
        dropped (connection lost before the request ran), lost (the request ran
        on hostB and the answer never came back), ok."""
        from xsm import remote
        real = subprocess.run

        def run(argv, input=None, **kw):
            if argv[0] != "ssh":                   # ps and the like still run
                return real(argv, input=input, **kw)
            if "-G" in argv:
                return subprocess.CompletedProcess(argv, 0, "hostname %s\n" % argv[-1], "")
            if how == "refused":
                return subprocess.CompletedProcess(
                    argv, 255, "", "ssh: connect to host hostB port 22: Connection refused\n")
            if how == "dropped":
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            request = json.loads(input)
            with self.on("hostB"):
                reply = remote.serve("hostA", request)
                if then:
                    then(request)
            if how == "lost":
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            if how == "reset":
                return subprocess.CompletedProcess(
                    argv, 255, "", "client_loop: send disconnect: Broken pipe\n")
            return subprocess.CompletedProcess(argv, 0, json.dumps(reply) + "\n", "")
        from unittest import mock
        return mock.patch.object(subprocess, "run", run)

    def send(self, how, then=None, msg_id=None, target="codex:alive"):
        from xsm import remote
        with self.ssh(how, then), self.on("hostA"):
            return remote.send(self.sender, target, "hostB", "hi", "task", None, 0,
                               msg_id=msg_id)

    def state(self, host, msg_id):
        from xsm import ledger
        with self.on(host):
            return ledger.status(msg_id)

    def reconcile(self, msg_id):
        from xsm import remote
        with self.ssh("ok"), self.on("hostA"):
            return remote.reconcile(msg_id)

    def gate_delivers(self, request):
        from xsm import ledger
        ledger.receipt(request["id"], "delivered", {"name": "alive", "alias": "codex"})

    def test_connection_refused_is_a_definite_error(self):
        reply = self.send("refused")
        self.assertEqual(reply["status"], "error", reply)
        self.assertEqual(self.state("hostA", reply["id"])["status"], "error",
                         "nothing left, so the ledger must not say queued")
        self.assertIn("Connection refused", self.state("hostA", reply["id"])["error"])
        self.assertEqual(self.state("hostB", reply["id"]), {})
        self.assertEqual(self.delivered, [])

    def test_lost_answer_is_unknown_until_the_peer_is_asked(self):
        reply = self.send("lost", then=self.gate_delivers)
        self.assertEqual(reply["status"], "unknown", reply)
        self.assertIn("xsm status %s" % reply["id"], reply["error"])
        self.assertEqual(self.state("hostA", reply["id"])["status"], "unknown")
        self.assertEqual(self.state("hostB", reply["id"])["status"], "delivered")

        after = self.reconcile(reply["id"])
        self.assertEqual(after["status"], "delivered", after)
        self.assertEqual(self.state("hostA", reply["id"])["status"], "delivered")
        self.assertTrue(after["receipt"]["receiver"]["alias"].endswith("@hostB"), after)

    def test_a_dropped_connection_after_login_is_unknown_not_error(self):
        reply = self.send("reset")
        self.assertEqual(reply["status"], "unknown", reply)
        after = self.reconcile(reply["id"])
        self.assertEqual(after["status"], "queued", "it reached hostB and waits there")
        self.assertNotIn("error", after)

    def test_a_request_that_never_ran_is_settled_as_error_by_asking(self):
        reply = self.send("dropped")
        self.assertEqual(reply["status"], "unknown", reply)
        after = self.reconcile(reply["id"])
        self.assertEqual(after["status"], "error", after)
        self.assertIn("no record", after["error"])

    def test_resending_the_same_id_does_not_deliver_twice(self):
        first = self.send("lost", then=self.gate_delivers)
        again = self.send("ok", msg_id=first["id"])
        self.assertEqual(again["status"], "delivered", again)
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(self.delivered, ["alive"], "handed over once")
        self.assertEqual(self.state("hostA", first["id"])["status"], "delivered")

    def test_a_peer_refusal_is_recorded_not_left_queued(self):
        reply = self.send("ok", target="codex:nobody")
        self.assertEqual(reply["status"], "refused", reply)
        self.assertEqual(self.state("hostA", reply["id"])["status"], "refused")

    def test_status_answers_only_for_the_asking_peers_own_messages(self):
        from xsm import ledger, remote
        sent = self.send("ok", then=self.gate_delivers)
        with self.on("hostB"):
            self.assertEqual(remote._serve("hostA", {"op": "status", "id": sent["id"]})["status"],
                             "delivered")
            self.assertIsNone(remote._serve("hostC", {"op": "status", "id": sent["id"]})["status"],
                              "another peer learns nothing about hostA's message")
            ledger.queued("local1", {"name": "x"}, {"name": "y"}, "project:demo", "note", "secret")
            ledger.receipt("local1", "delivered", {"name": "y"})
            self.assertIsNone(remote._serve("hostA", {"op": "status", "id": "local1"})["status"])
            self.assertIsNone(remote._serve("hostA", {"op": "status", "id": "../config"})["status"])
        taken = self.send("ok", msg_id="local1")
        self.assertEqual(taken["status"], "refused", "a peer cannot reuse a local id")
        with self.on("hostB"):
            self.assertEqual(ledger.status("local1")["preview"], "secret")

    def test_cli_status_and_ledger_for_a_remote_message(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        from xsm import cli, ledger
        reply = self.send("lost", then=self.gate_delivers)
        with self.on("hostA"):
            rows = cli._mark_undelivered(ledger.recent(10))
        self.assertEqual(rows[0]["status"], "unknown", "not relabelled undelivered")
        out = io.StringIO()
        with self.ssh("ok"), self.on("hostA"), redirect_stdout(out):
            code = cli.cmd_status(argparse.Namespace(msg_id=reply["id"], wait=0, json=False))
        self.assertEqual(code, cli.OK, out.getvalue())
        self.assertTrue(out.getvalue().startswith("delivered"), out.getvalue())

    def test_a_queued_remote_row_is_not_called_undelivered(self):
        from xsm import cli, ledger
        reply = self.send("ok")
        with self.on("hostA"):
            row = [r for r in cli._mark_undelivered(ledger.recent(10)) if r["id"] == reply["id"]][0]
        self.assertEqual(row["status"], "queued")
        self.assertIn("xsm status %s" % reply["id"], row["note"])


if __name__ == "__main__":
    unittest.main()
