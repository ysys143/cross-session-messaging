"""Channels (ADR-0005): the record sessions and people share, and decisions
that only a person makes."""
import io
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_xsm import TempState  # noqa: E402

AGENT = {"kind": "agent", "name": "builder", "alias": "claude-4", "ref": "aaaaaa",
         "runtime": "claude"}
PERSON = {"kind": "human", "name": "me"}


class ChannelTest(TempState):
    def _here(self, name="proj"):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    def test_threads_tags_and_months(self):
        from xsm import channel
        where = channel.resolve(self._here())
        q = channel.post(where, AGENT, "Which cache?", "question")
        channel.post(where, PERSON, "redis", "note", reply_to=q["id"])
        text = channel.render(channel.read(where[1]))
        lines = text.splitlines()
        self.assertIn("[question] Which cache?", lines[0])
        self.assertTrue(lines[1].startswith("    "), "a reply sits under its root")
        self.assertEqual(channel.render(channel.read(where[1]), tag="note").count("\n"), 0)

    def test_a_decision_is_a_persons(self):
        from xsm import channel
        where = channel.resolve(self._here())
        with self.assertRaises(channel.ChannelError) as cm:
            channel.post(where, AGENT, "we use redis", "decision")
        self.assertIn("xsm_decide", str(cm.exception))
        channel.post(where, PERSON, "we use redis", "decision")

    def test_channels_follow_scope(self):
        from xsm import channel, config
        a, b = self._here("a"), self._here("b")
        self.assertNotEqual(channel.resolve(a)[1], channel.resolve(b)[1])
        with self.assertRaises(channel.ChannelError):
            channel.resolve(a, "demo")
        config.join("demo", a)
        config.join("demo", b)
        self.assertEqual(channel.resolve(a, "demo"), channel.resolve(b, "demo"))

    def test_same_basename_repositories_do_not_share_a_channel(self):
        from xsm import channel
        one, two = os.path.join(self.tmp, "x", "app"), os.path.join(self.tmp, "y", "app")
        for d in (one, two):
            os.makedirs(d)
            subprocess.run(["git", "init", "-q", d], check=True)
        self.assertEqual(channel.resolve(one)[0], channel.resolve(two)[0])     # both repo:app
        self.assertNotEqual(channel.resolve(one)[1], channel.resolve(two)[1])

    def test_concurrent_appends_keep_every_record(self):
        from xsm import channel
        here = self._here()
        code = ("import sys; sys.path.insert(0, %r); import os; os.environ['XSM_HOME']=%r\n"
                "from xsm import paths, channel; paths.HOME=%r\n"
                "w = channel.resolve(%r)\n"
                "[channel.post(w, {'kind':'agent','name':'p','alias':'a','ref':'r'}, 'x'*3000)"
                " for _ in range(50)]") % (os.path.dirname(os.path.dirname(os.path.abspath(
                    __file__))), self.tmp, self.tmp, here)
        procs = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(8)]
        for p in procs:
            p.wait()
        self.assertEqual(len(channel.read(channel.resolve(here)[1])), 400)

    def test_export_carries_the_question_and_answer(self):
        from xsm import channel
        where = channel.resolve(self._here())
        channel.post(where, dict(PERSON, via="mcp-elicitation"), "cache: redis", "decision",
                     approved={"question": "Which cache?", "answer": "redis"})
        md = channel.export_markdown(where[0], channel.read(where[1]))
        self.assertIn("- Asked: Which cache?", md)
        self.assertIn("- Answer: redis", md)


class McpServerTest(TempState):
    """The server speaks MCP over stdio and records only what the person chose."""

    def _run(self, *messages, session=AGENT, cwd=None):
        from xsm import mcp
        here = cwd or os.path.join(self.tmp, "proj")
        os.makedirs(here, exist_ok=True)
        inp = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        out = io.StringIO()
        server = mcp.Server(inp, out)
        server.session = lambda: dict(session, cwd=here) if session else None
        server.serve()
        return [json.loads(l) for l in out.getvalue().splitlines()], here

    INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {"elicitation": {}}}}

    def test_decide_asks_the_person_and_records_their_answer(self):
        from xsm import channel
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "xsm_decide", "arguments": {"question": "Which queue?",
                                                "options": ["sqs", "kafka"], "summary": "queue"}}}
        answer = {"jsonrpc": "2.0", "id": "xsm-1", "result": {"action": "accept",
                                                              "content": {"answer": "kafka"}}}
        out, here = self._run(self.INIT, call, answer)
        elicit = next(m for m in out if m.get("method") == "elicitation/create")
        self.assertEqual(elicit["params"]["requestedSchema"]["properties"]["answer"]["enum"],
                         ["sqs", "kafka"])
        rec = channel.read(channel.resolve(here)[1])[-1]
        self.assertEqual((rec["tag"], rec["text"], rec["author"]["kind"]),
                         ("decision", "queue: kafka", "human"))
        self.assertEqual(rec["approved"], {"question": "Which queue?", "answer": "kafka",
                                           "options": ["sqs", "kafka"]})

    def test_a_declined_question_records_nothing(self):
        from xsm import channel
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "xsm_decide", "arguments": {"question": "Ship it?"}}}
        answer = {"jsonrpc": "2.0", "id": "xsm-1", "result": {"action": "decline"}}
        out, here = self._run(self.INIT, call, answer)
        reply = next(m for m in out if m.get("id") == 2)
        self.assertIn("did not answer", reply["result"]["content"][0]["text"])
        self.assertEqual(channel.read(channel.resolve(here)[1]), [])

    def test_post_cannot_make_a_decision_and_needs_a_session(self):
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "xsm_post", "arguments": {"text": "we chose x", "tag": "decision"}}}
        out, _ = self._run(self.INIT, call)
        self.assertTrue(next(m for m in out if m.get("id") == 2)["result"]["isError"])
        out, _ = self._run(self.INIT, dict(call, params={"name": "xsm_post",
                                                         "arguments": {"text": "hi"}}),
                           session=None)
        self.assertIn("not registered", next(m for m in out if m.get("id") == 2)
                      ["result"]["content"][0]["text"])

    def test_without_elicitation_support_decide_refuses(self):
        init = dict(self.INIT, params={"protocolVersion": "2025-06-18", "capabilities": {}})
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "xsm_decide", "arguments": {"question": "?"}}}
        out, _ = self._run(init, call)
        self.assertIn("cannot ask its user",
                      next(m for m in out if m.get("id") == 2)["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
