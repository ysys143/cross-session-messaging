"""Who the MCP server signs a call as (Server.session). Codex threads of one
CODEX_HOME share the app-server daemon's pid, so the pid cannot tell them
apart; Codex 0.158 names the thread in `params._meta.threadId` (measured
2026-09-29)."""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xsm import mcp, paths, registry  # noqa: E402

DAEMON = 4242


def row(runtime, session_id, updated, pid=DAEMON, state="live"):
    return {"runtime": runtime, "session_id": session_id, "pid": pid, "state": state,
            "updated": updated, "name": session_id, "cwd": "/tmp"}


def serve_one(records, meta=None, pid=DAEMON):
    """Send one tools/call; return the `me` the server resolved and its reply."""
    seen = []
    params = {"name": "xsm_inbox", "arguments": {}}
    if meta is not None:
        params["_meta"] = meta
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    out = io.StringIO()
    server = mcp.Server(io.StringIO(json.dumps(msg) + "\n"), out)

    def call(name, args):
        seen.append(server.session())
        return "ok"

    server.call = call
    with mock.patch.object(mcp.identity, "ancestor_pid", return_value=pid), \
            mock.patch.object(mcp.registry, "records", return_value=records), \
            mock.patch.object(mcp.registry, "adopt_codex_thread", return_value=None):
        server.serve()
    return seen, json.loads(out.getvalue())["result"]


class McpCallerIdentityTest(unittest.TestCase):
    TWO = [row("codex", "thread-a", 100), row("codex", "thread-b", 200)]

    def test_thread_named_in_the_call_wins_over_the_newest_record(self):
        seen, result = serve_one(self.TWO, meta={"threadId": "thread-a"})
        self.assertFalse(result["isError"])
        self.assertEqual(seen[0]["session_id"], "thread-a")
        seen, _ = serve_one(self.TWO, meta={"threadId": "thread-b"})
        self.assertEqual(seen[0]["session_id"], "thread-b")

    def test_each_call_uses_its_own_thread(self):
        metas = [{"threadId": "thread-a"}, {}, {"threadId": "thread-b"}]
        msgs = [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
                 "params": {"name": "xsm_inbox", "_meta": m}} for i, m in enumerate(metas)]
        server = mcp.Server(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), io.StringIO())
        got = []
        server.call = lambda name, args: got.append(server.call_thread) or "ok"
        server.serve()
        self.assertEqual(got, ["thread-a", None, "thread-b"])

    def test_unknown_thread_that_cannot_be_adopted_uses_the_most_recent(self):
        # Untrusted home / thread in no state DB: connect as the newest record
        # rather than refuse (user decision, 2026-09-30).
        seen, result = serve_one(self.TWO, meta={"threadId": "thread-z"})
        self.assertFalse(result["isError"])
        self.assertEqual(seen[0]["session_id"], "thread-b")

    def test_ended_thread_record_is_not_used(self):
        recs = [row("codex", "thread-a", 100, state="ended"), row("codex", "thread-b", 200)]
        seen, _ = serve_one(recs, meta={"threadId": "thread-a"})
        self.assertEqual(seen[0]["session_id"], "thread-b")

    def test_several_threads_without_a_thread_id_use_the_most_recent(self):
        seen, result = serve_one(self.TWO)
        self.assertFalse(result["isError"])
        self.assertEqual(seen[0]["session_id"], "thread-b")

    def test_single_codex_thread_needs_no_thread_id(self):
        seen, result = serve_one([row("codex", "thread-a", 100)])
        self.assertFalse(result["isError"])
        self.assertEqual(seen[0]["session_id"], "thread-a")

    def test_claude_path_is_unchanged(self):
        recs = [row("claude", "old", 100), row("claude", "new", 200),
                row("claude", "other-pid", 300, pid=1)]
        seen, _ = serve_one(recs)
        self.assertEqual(seen[0]["session_id"], "new")
        # Claude sends no thread id; a stray one does not change who it is.
        seen, result = serve_one(recs, meta={"threadId": "old"})
        self.assertFalse(result["isError"])
        self.assertEqual(seen[0]["session_id"], "new")

    def test_no_session_process_means_no_identity(self):
        seen, _ = serve_one(self.TWO, pid=None)
        self.assertEqual(seen, [None])


class AdoptUnknownThreadTest(unittest.TestCase):
    """A thread with no live record is registered on the spot (real registry
    in a temp XSM_HOME, fake Codex home with a state DB)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xsm-mcpid-")
        self._old_home = paths.HOME
        paths.HOME = os.path.join(self.tmp, "xsm")
        paths.ensure_home()
        self.addCleanup(setattr, paths, "HOME", self._old_home)
        self.home = os.path.realpath(os.path.join(self.tmp, "codex"))
        os.makedirs(self.home)
        con = sqlite3.connect(os.path.join(self.home, "state_5.sqlite"))
        con.execute("create table threads (id text, name text, cwd text, source text)")
        con.execute("insert into threads values ('new-thread', 'Fresh', ?, 'cli')",
                    (os.path.join(self.tmp, "proj"),))
        con.execute("insert into threads values ('sub-thread', 'sub', '/x', ?)",
                    (json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "thread-a"}}}),))
        con.execute("insert into threads values ('orphan', 'orphan', '/x', ?)",
                    (json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "gone"}}}),))
        con.commit()
        con.close()
        self.env = mock.patch.dict(os.environ, {"CODEX_HOME": self.home})
        self.env.start()
        self.addCleanup(self.env.stop)

    def serve(self, thread, records, trusted=True):
        seen = []
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "xsm_inbox", "_meta": {"threadId": thread}}}
        server = mcp.Server(io.StringIO(json.dumps(msg) + "\n"), io.StringIO())
        server.call = lambda name, args: seen.append(server.session()) or "ok"
        with mock.patch.object(mcp.identity, "ancestor_pid", return_value=DAEMON), \
                mock.patch.object(registry, "_hooks_will_register", return_value=trusted), \
                mock.patch.object(registry.identity, "is_app_server", return_value=True), \
                mock.patch.object(registry, "records", return_value=records):
            server.serve()
        return seen[0]

    def test_unknown_thread_in_a_trusted_home_is_registered_and_used(self):
        me = self.serve("new-thread", [row("codex", "thread-b", 200)])
        self.assertEqual(me["session_id"], "new-thread")
        self.assertEqual(me["home"], self.home)
        self.assertEqual(me["cwd"], os.path.realpath(os.path.join(self.tmp, "proj")))
        self.assertTrue(me["adopted"])
        self.assertEqual(me["pid"], DAEMON)
        self.assertTrue(me["app_server"])
        stored = paths.read_json(registry._record_path("codex", "new-thread"))
        self.assertEqual(stored["session_id"], "new-thread")
        self.assertTrue(stored["adopted"])

    def test_first_thread_of_a_daemon_with_no_records_is_registered(self):
        me = self.serve("new-thread", [])
        self.assertEqual(me["session_id"], "new-thread")

    def test_unknown_thread_in_an_untrusted_home_falls_back_to_the_most_recent(self):
        me = self.serve("new-thread", [row("codex", "thread-a", 100), row("codex", "thread-b", 200)],
                        trusted=False)
        self.assertEqual(me["session_id"], "thread-b")
        self.assertIsNone(paths.read_json(registry._record_path("codex", "new-thread")))

    def test_sub_agent_signs_as_its_live_parent(self):
        me = self.serve("sub-thread", [row("codex", "thread-a", 100), row("codex", "thread-b", 200)])
        self.assertEqual(me["session_id"], "thread-a")
        self.assertIsNone(paths.read_json(registry._record_path("codex", "sub-thread")))

    def test_orphan_sub_agent_is_registered_as_itself(self):
        me = self.serve("orphan", [row("codex", "thread-b", 200)])
        self.assertEqual(me["session_id"], "orphan")

    def test_exact_match_is_not_adopted_over(self):
        me = self.serve("thread-a", [row("codex", "thread-a", 100), row("codex", "thread-b", 200)])
        self.assertEqual(me["session_id"], "thread-a")
        self.assertNotIn("adopted", me)


if __name__ == "__main__":
    unittest.main()
