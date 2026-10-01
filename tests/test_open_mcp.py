"""The MCP tools and the CLI under the audit of 2026-10-01: xsm_send says what
the CLI says and waits as its schema says, the tools say what they do (Codex
declines one that says nothing), and a refused write is a sentence."""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_xsm  # noqa: E402

# Module names only: a class imported here by name would run its tests twice.
TempState = test_xsm.TempState
REPO = test_xsm.REPO


def _run_cli(*argv):
    from xsm import cli
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue() + err.getvalue()


class McpSendTest(TempState):
    """A6: xsm_send says what the CLI says, and waits as its schema says."""

    def _call(self, args, send_result=None):
        from xsm import mcp, send
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"capabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "xsm_send", "arguments": args}}]
        out = io.StringIO()
        server = mcp.Server(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        server.session = lambda: {"name": "me", "alias": "h", "ref": "aaaaaa", "runtime": "claude",
                                  "cwd": self.tmp}
        seen = {}

        def fake(target, text, **kw):
            seen.update(kw)
            return send_result
        with mock.patch.object(send, "send", fake):
            server.serve()
        reply = next(m for m in map(json.loads, out.getvalue().splitlines()) if m.get("id") == 2)
        return reply["result"]["content"][0]["text"], seen

    def test_a_refusal_carries_its_candidates_and_notes_like_the_cli(self):
        from xsm import send
        result = send.SendResult("refused", "no session named 'x'", candidates=[
            {"name": "alpha", "alias": "claude", "ref": "aaaaaa", "runtime": "claude",
             "state": "live", "cwd": "/p"}])
        result.notes = ["connected first"]
        text, _ = self._call({"target": "x", "text": "hi"}, result)
        lines = text.splitlines()
        self.assertEqual(lines[0], "connected first")
        self.assertEqual(lines[1], "refused: no session named 'x'")
        self.assertEqual(lines[2], "registered sessions right now:")
        self.assertIn("alpha@claude [aaaaaa]", text)

    def test_the_mcp_text_is_the_cli_text(self):
        from xsm import send
        for result in (send.SendResult("delivered", "receiver recorded it", "m1"),
                       send.SendResult("refused", "", "m2"),
                       send.SendResult("refused", "2 sessions match 'x'", candidates=[
                           {"name": "a", "alias": "h", "ref": "bbbbbb", "runtime": "codex",
                            "state": "live", "cwd": "/q"}])):
            with self.subTest(result.reason):
                text, _ = self._call({"target": "x", "text": "hi"}, result)
                self.assertEqual(text, "\n".join(result.lines()))

    def test_candidates_are_not_listed_when_the_reason_already_says_what_to_do(self):
        from xsm import send
        result = send.SendResult("refused", "x is open but has not registered with xsm: why",
                                 candidates=[{"name": "a", "alias": "h", "ref": "bbbbbb"}])
        self.assertEqual(self._call({"target": "x", "text": "hi"}, result)[0],
                         "refused: x is open but has not registered with xsm: why")

    def test_wait_defaults_to_the_schemas_15_and_a_given_value_stands(self):
        from xsm import mcp, send
        result = send.SendResult("delivered", "ok")
        schema = next(t for t in mcp.TOOLS if t["name"] == "xsm_send")["inputSchema"]
        self.assertEqual(schema["properties"]["wait"]["default"], 15)
        self.assertEqual(self._call({"target": "x", "text": "hi"}, result)[1]["wait"], 15.0)
        self.assertEqual(self._call({"target": "x", "text": "hi", "wait": 0}, result)[1]["wait"], 0.0)
        self.assertEqual(self._call({"target": "x", "text": "hi", "wait": 3}, result)[1]["wait"], 3.0)
        self.assertEqual(self._call({"target": "x", "text": "hi", "wait": None}, result)[1]["wait"],
                         15.0)


class ToolAnnotationsTest(TempState):
    """B6: Codex under approval_policy "never" declines a tool that says nothing."""

    def _tools(self):
        from xsm import mcp
        out = io.StringIO()
        mcp.Server(io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                               + "\n"), out).serve()
        return {t["name"]: t for t in json.loads(out.getvalue().splitlines()[0])["result"]["tools"]}

    def test_the_messaging_tools_say_what_they_do(self):
        tools = self._tools()
        self.assertEqual(tools["xsm_channel"]["annotations"],
                         {"readOnlyHint": True, "openWorldHint": False})
        for name in ("xsm_send", "xsm_post", "xsm_inbox"):
            self.assertEqual(tools[name]["annotations"],
                             {"readOnlyHint": False, "destructiveHint": False,
                              "openWorldHint": False}, name)

    def test_the_form_tools_are_left_to_their_own_forms(self):
        tools = self._tools()
        for name in ("xsm_link", "xsm_reach", "xsm_join", "xsm_approve", "xsm_grant",
                     "xsm_decide", "xsm_doc_endorse"):
            self.assertNotIn("annotations", tools[name], name)

    def test_the_codex_server_config_is_not_changed_by_it(self):
        with open(os.path.join(REPO, "hooks", "codex-mcp.json")) as fh:
            self.assertEqual(json.load(fh),
                             {"mcpServers": {"xsm": {"command": "./hooks/xsm-mcp", "cwd": "."}}})


class SandboxTest(TempState):
    """B3: a state folder that cannot be written is a sentence, not a traceback."""

    def _denied(self, path="/x/.xsm/ledger/m.json"):
        err = PermissionError(1, "Operation not permitted", path)
        return err

    def test_the_sentence_names_the_file_the_cause_and_the_mcp_tool(self):
        from xsm import paths
        text = paths.sandbox_blocked(self._denied())
        self.assertTrue(text.startswith("sandbox-blocked: "), text)
        self.assertIn("/x/.xsm/ledger/m.json", text)
        self.assertIn("Operation not permitted", text)
        self.assertIn("xsm_send", text)
        self.assertTrue(paths.blocked_write(self._denied()))
        self.assertTrue(paths.blocked_write(OSError(30, "Read-only file system")))
        self.assertFalse(paths.blocked_write(FileNotFoundError(2, "No such file")))

    def test_a_send_whose_ledger_cannot_be_written_is_an_error_that_says_so(self):
        from xsm import ledger, registry, send
        registry.upsert("claude", os.path.join(self.tmp, "homes", "c"), "s-a", os.getpid(),
                        self.tmp, name="a")
        registry.upsert("claude", os.path.join(self.tmp, "homes", "c"), "s-b", os.getpid(),
                        self.tmp, name="b")
        me, you = registry.by_session("claude", "s-a"), registry.by_session("claude", "s-b")
        you["socket"] = "/tmp/none.sock"
        with mock.patch.object(ledger, "queued", side_effect=self._denied()), \
                mock.patch.object(send.resolve, "resolve",
                                  return_value=mock.Mock(ok=True, record=you)), \
                mock.patch.object(send, "not_running", return_value=None):
            result = send.send("b", "hi", sender=me)
        self.assertEqual(result.status, "error")
        self.assertTrue(result.reason.startswith("sandbox-blocked: "), result.reason)
        self.assertIn("xsm_send", result.reason)
        self.assertEqual(result.lines()[-1], "error: " + result.reason)

    def test_a_write_that_is_not_a_permission_problem_still_raises(self):
        from xsm import registry, send
        registry.upsert("claude", os.path.join(self.tmp, "homes", "c"), "s-a", os.getpid(),
                        self.tmp, name="a")
        me = registry.by_session("claude", "s-a")
        with mock.patch.object(send, "_send", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                send.send("b", "hi", sender=me)

    def test_the_cli_survives_a_state_folder_it_cannot_make(self):
        from xsm import cli, paths
        with mock.patch.object(paths, "ensure_home", side_effect=self._denied(self.tmp)):
            code, text = _run_cli("who", "--json")
        self.assertNotIn("Traceback", text)
        with mock.patch.object(paths, "ensure_home", side_effect=self._denied(self.tmp)), \
                mock.patch.object(cli, "cmd_who", side_effect=self._denied("/x/.xsm/y")):
            code, text = _run_cli("who")
        self.assertEqual(code, cli.REFUSED)
        self.assertIn("sandbox-blocked: xsm cannot use /x/.xsm/y", text)
        self.assertNotIn("Traceback", text)

    def test_the_cli_does_not_hide_an_error_that_is_not_a_permission_problem(self):
        from xsm import cli, paths
        with mock.patch.object(paths, "ensure_home", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                _run_cli("who")

    def test_the_mcp_server_still_answers_initialize_when_the_beacon_cannot_be_written(self):
        from xsm import mcp
        out = io.StringIO()
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"capabilities": {}}}) + "\n"
        real = mcp.Server
        with mock.patch.object(mcp, "write_beacon", side_effect=self._denied()), \
                mock.patch.object(mcp, "preload", return_value=[]), \
                mock.patch.object(mcp, "Server", lambda: real(io.StringIO(init), out)):
            self.assertEqual(mcp.main(), 0)
        self.assertEqual(json.loads(out.getvalue().splitlines()[0])["id"], 1)
        self.assertIn("serverInfo", out.getvalue())

    def test_a_tool_that_hits_a_refused_write_says_so(self):
        from xsm import channel, mcp
        msgs = [{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "xsm_post", "arguments": {"text": "x"}}}]
        out = io.StringIO()
        server = mcp.Server(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        server.session = lambda: {"name": "me", "alias": "h", "ref": "aaaaaa", "cwd": self.tmp}
        with mock.patch.object(channel, "post", side_effect=self._denied("/ro/channel.jsonl")):
            server.serve()
        reply = json.loads(out.getvalue().splitlines()[0])["result"]
        self.assertTrue(reply["isError"])
        self.assertIn("sandbox-blocked: xsm cannot use /ro/channel.jsonl",
                      reply["content"][0]["text"])

    def test_the_inbox_socket_hint_names_the_mcp_tool(self):
        from xsm import adapters
        with mock.patch.object(adapters.socket, "socket") as sock:
            sock.return_value.__enter__.return_value.connect.side_effect = PermissionError(
                1, "Operation not permitted")
            with self.assertRaises(adapters.DeliveryError) as raised:
                adapters.to_claude("/tmp/x.sock", "c", "m1")
        self.assertEqual(raised.exception.reason, "sandbox-blocked")
        self.assertIn("xsm_send MCP tool", raised.exception.detail)
        self.assertNotIn("trusted hook", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
