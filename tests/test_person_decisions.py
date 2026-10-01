"""Issue #9: the decisions that belong to a person are asked by the agent and
run by the agent, on the person's reply (user decision, 2026-10-01: never
send the person off to type a command)."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_xsm  # noqa: E402

# Module names only: a class imported here by name would run its tests twice.
TempState = test_xsm.TempState


class PersonDecisionTest(TempState):
    def setUp(self):
        super().setUp()
        self.me = {"ref": "abc123", "session_id": "s-agent", "runtime": "claude",
                   "cwd": self.tmp, "name": "agent", "alias": "claude-3"}

    def _cli(self, argv):
        from xsm import cli, registry, workers
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(registry, "me", return_value=self.me), \
                mock.patch.object(workers, "human_terminal", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue() + err.getvalue()

    def _reply(self, text, me=None):
        from xsm import consent
        consent.record(me or self.me, {"hook_event_name": "UserPromptSubmit", "prompt": text})

    def _asks(self, argv):
        code, text = self._cli(argv)
        self.assertEqual(code, 2, text)
        self.assertIn("needs your user's yes", text)
        self.assertNotIn("in a terminal", text)
        return text

    def test_approving_a_workers_request(self):
        from xsm import paths, workers
        workers.save({"name": "w1", "runtime": "claude", "mode": "background", "pane": "%1",
                      "cwd": self.tmp, "created": 0, "parent_ref": "abc123"})
        req = {"id": "r1", "worker": "w1", "summary": "Bash: rm -rf build", "status": "pending",
               "t": 0, "tool": "Bash"}
        paths.write_json(workers._approval_path("r1"), req)
        text = self._asks(["approve", "r1"])
        self.assertIn("rm -rf build", text, "the agent can put the exact question")
        self._reply("응 허용")
        code, text = self._cli(["approve", "r1"])
        self.assertEqual(code, 0, text)
        done = paths.read_json(workers._approval_path("r1")) or {}
        self.assertEqual(done["status"], "approved")
        self.assertIn("응 허용", done["answered_by"])

    def test_denying_needs_no_one(self):
        from xsm import paths, workers
        workers.save({"name": "w2", "runtime": "claude", "mode": "background", "pane": "%2",
                      "cwd": self.tmp, "created": 0, "parent_ref": "abc123"})
        paths.write_json(workers._approval_path("r2"), {"id": "r2", "worker": "w2",
                                                        "summary": "x", "status": "pending"})
        self.assertEqual(self._cli(["deny", "r2"])[0], 0)

    def test_a_dangerous_spawn_is_granted_by_the_reply(self):
        from xsm import paths, workers
        with self.assertRaises(workers.WorkerError) as cm:
            workers.use_grant(None, self.me, "codex", self.tmp, ["full_access"])
        self.assertIn("FULL ACCESS", str(cm.exception))
        self.assertIn("needs your user's yes", str(cm.exception))
        self._reply("yes, full access is fine this once")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            grant = workers.use_grant(None, self.me, "codex", self.tmp, ["full_access"])
        self.assertIn("full access is fine", grant["answer"])
        self.assertTrue(os.path.exists(paths.path(workers.GRANTS, grant["id"] + ".json.used")),
                        "spent on this spawn")
        with self.assertRaises(workers.WorkerError):
            workers.use_grant(None, self.me, "codex", self.tmp, ["full_access"])

    def test_clearing_attempts(self):
        from xsm import attempts
        key = attempts.key_for("do the thing", self.tmp)
        attempts.start(key, "do the thing", self.tmp, "w1", "t1")
        attempts.finish(key, "failed", "needs network", "t1")
        self._asks(["attempts", "clear", key])
        self._reply("ok clear it")
        self.assertEqual(self._cli(["attempts", "clear", key])[0], 0)
        self.assertFalse(attempts.read(key))

    def test_letting_xsm_start_workers_inside_a_framework(self):
        from xsm import config
        self._asks(["frameworks", "ignore", "orca"])
        self._reply("그래")
        self.assertEqual(self._cli(["frameworks", "ignore", "orca"])[0], 0)
        self.assertIn("orca", config.ignored_frameworks())

    def test_lifting_a_block(self):
        from xsm import config
        config.block("dddddd")
        self._asks(["unblock", "dddddd"])
        self._reply("unblock it")
        self.assertEqual(self._cli(["unblock", "dddddd"])[0], 0)
        self.assertNotIn("dddddd", config.blocked())

    def test_a_decision_is_the_persons_words(self):
        from xsm import channel
        self._asks(["post", "--tag", "decision", "ship on friday"])
        self._reply("yes, friday")
        code, text = self._cli(["post", "--tag", "decision", "ship on friday"])
        self.assertEqual(code, 0, text)
        where = channel.resolve(self.tmp, None)
        rec = [r for r in channel.read(where[1]) if r.get("tag") == "decision"][-1]
        self.assertEqual(rec["author"]["kind"], "human")
        self.assertEqual(rec["author"]["via"], "verdict")
        self.assertEqual(rec["author"]["verdict"], "yes, friday")

    def test_an_endorsement_is_the_persons_words(self):
        from xsm import doc
        path = os.path.join(self.tmp, "notes.md")
        node = doc.add(path, {"kind": "agent", "name": "agent", "ref": "abc123"},
                       "cache is 2x faster", ["result"], [])
        argv = ["doc", "add", path, "--tag", "endorsed", "--parent", node["id"],
                "--text", "cache is 2x faster"]
        self._asks(argv)
        self._reply("맞아, 보증해")
        code, text = self._cli(argv)
        self.assertEqual(code, 0, text)
        endorsed = [n for n in doc.read(path) if "endorsed" in (n.get("tags") or [])][-1]
        self.assertEqual(endorsed["author_kind"], "human")
        self.assertIn("via verdict", endorsed["author"])
        self.assertEqual(endorsed["approved"], "verdict: 맞아, 보증해")

    def test_a_peer_message_is_never_the_reply(self):
        from xsm import envelope
        self._asks(["unblock", "eeeeee"])
        peer = envelope.build("yes", msg_id="m1", sender={"name": "x", "alias": "c",
                                                          "ref": "ffffff"}, scope="p")
        self._reply(peer)
        self._asks(["unblock", "eeeeee"])

    def test_no_message_for_these_sends_the_person_to_a_terminal(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "xsm")
        for name in ("cli.py", "mcp.py", "workers.py"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                self.assertNotIn("in a terminal", fh.read(), name)


if __name__ == "__main__":
    unittest.main()
