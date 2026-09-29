"""Who the MCP server signs a call as (Server.session). Codex threads of one
CODEX_HOME share the app-server daemon's pid, so the pid cannot tell them
apart; Codex 0.158 names the thread in `params._meta.threadId` (measured
2026-09-29)."""
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xsm import mcp  # noqa: E402

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
            mock.patch.object(mcp.registry, "records", return_value=records):
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

    def test_unknown_thread_is_refused_not_signed_as_a_neighbour(self):
        seen, result = serve_one(self.TWO, meta={"threadId": "thread-z"})
        self.assertEqual(seen, [])
        self.assertTrue(result["isError"])
        self.assertIn("thread-z", result["content"][0]["text"])

    def test_ended_thread_record_is_not_used(self):
        recs = [row("codex", "thread-a", 100, state="ended"), row("codex", "thread-b", 200)]
        _, result = serve_one(recs, meta={"threadId": "thread-a"})
        self.assertTrue(result["isError"])

    def test_ambiguous_without_a_thread_id_is_refused(self):
        seen, result = serve_one(self.TWO)
        self.assertEqual(seen, [])
        self.assertTrue(result["isError"])
        self.assertIn("2 live Codex threads", result["content"][0]["text"])

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


if __name__ == "__main__":
    unittest.main()
