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
print(remote.add("hostB", "demo")["peer"])""")
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

    def test_unpaired_and_wrong_project_are_refused(self):
        refused = self.send("hostA", "agent@claude@hostB", "x")
        self.assertNotIn(refused["status"], ("sent-unconfirmed", "delivered"),
                         "not paired: the @hostB is not a remote, so nothing goes out")
        self.py("hostA", 'from xsm import remote; remote.add("hostB", "demo")')
        self.py("hostB", 'from xsm import config; config.leave("demo", %r)' % self.m["hostB"]["proj"])
        out = self.send("hostA", "agent@claude@hostB", "x")
        self.assertEqual(out["status"], "refused")
        self.assertIn("not in project demo", out["reason"])


if __name__ == "__main__":
    unittest.main()
