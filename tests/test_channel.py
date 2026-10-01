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

    def test_a_refusal_says_who_answered(self):
        from xsm import mcp
        said = lambda result: mcp.who_answered({"result": result})
        self.assertIn("automatic reviewer", said({"action": "decline", "_meta": {
            "approvals_reviewer": "auto_review", "message": "looks risky"}}))
        self.assertIn("looks risky", said({"action": "decline", "_meta": {
            "approvals_reviewer": "auto_review", "message": "looks risky"}}))
        self.assertIn("approval_policy", said({"action": "decline"}))
        self.assertIn("no choice", said({"action": "accept", "content": {}}))
        self.assertIn("'deny'", said({"action": "accept", "content": {"answer": "deny"}}))
        self.assertIn("dismissed", said({"action": "cancel"}))

    def test_a_join_answered_by_codex_itself_is_not_taken_for_the_user(self):
        from xsm import config
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "xsm_join", "arguments": {"project": "demo"}}}
        auto = {"jsonrpc": "2.0", "id": "xsm-1", "result": {
            "action": "accept", "content": {}, "_meta": {"approvals_reviewer": "auto_review"}}}
        out, _ = self._run(self.INIT, call, auto)
        text = next(m for m in out if m.get("id") == 2)["result"]["content"][0]["text"]
        self.assertIn("automatic reviewer", text)
        self.assertIn("xsm join demo", text)            # what the person can do instead
        self.assertEqual(config.projects(), [])

    def test_reach_asks_the_person_and_records_only_an_allow(self):
        from xsm import config, registry
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        # A reach is bound to a running session's record, so the asker must be
        # one (a Codex record with a live pid reads live without a socket).
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        rec = registry.upsert("codex", home, "t-reach", os.getpid(), self.tmp, name="builder")
        agent = dict(AGENT, ref=rec["ref"])
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "xsm_reach", "arguments": {"dir": other, "reason": "hand over a task"}}}
        deny = {"jsonrpc": "2.0", "id": "xsm-1", "result": {"action": "accept",
                                                            "content": {"answer": "deny"}}}
        out, _ = self._run(self.INIT, call, deny, session=agent)
        self.assertIn("other", next(m for m in out if m.get("method") == "elicitation/create")
                      ["params"]["message"])
        self.assertEqual(config.reaches(), [])
        allow = dict(deny, result={"action": "accept", "content": {"answer": "allow"}})
        out, _ = self._run(self.INIT, call, allow, session=agent)
        self.assertIn("allowed", next(m for m in out if m.get("id") == 2)
                      ["result"]["content"][0]["text"])
        self.assertEqual([(r["ref"], r["root"]) for r in config.reaches()],
                         [(agent["ref"], os.path.realpath(other))])

    def _six(self, here):
        """The asking session, and each form tool with its arguments, the answer
        that would make it act, and what it changed — so a reply can be checked
        against all six at once. The session is a running record, so a reach
        allowed here is really added (test_a_persons_choice_acts_in_all_six)."""
        from xsm import channel, config, doc, paths, registry, workers
        other = os.path.join(self.tmp, "other")
        os.makedirs(other, exist_ok=True)
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        rec = registry.upsert("codex", home, "t-six", os.getpid(), self.tmp, name="builder")
        agent = dict(AGENT, ref=rec["ref"])
        node = doc.add(os.path.join(here, "d.md"), AGENT, "the report", ["report"])
        workers.save({"name": "w1", "parent_ref": agent["ref"], "created": 0})
        os.makedirs(paths.path(workers.APPROVALS), exist_ok=True)
        paths.write_json(paths.path(workers.APPROVALS, "r1.json"),
                         {"id": "r1", "worker": "w1", "status": "pending", "t": 1, "summary": "x"})
        decisions = lambda: [r for r in channel.read(channel.resolve(here)[1])
                             if r["tag"] == "decision"]
        grants = lambda: os.listdir(paths.path(workers.GRANTS)) \
            if os.path.isdir(paths.path(workers.GRANTS)) else []
        return agent, [
            ("xsm_decide", {"question": "Ship?", "options": ["yes", "no"]}, "yes", decisions),
            ("xsm_doc_endorse", {"doc": "d.md", "node": node["id"]}, "endorse",
             lambda: [n for n in doc.read(os.path.join(here, "d.md")) if "endorsed" in n["tags"]]),
            ("xsm_join", {"project": "demo"}, "allow", config.projects),
            ("xsm_reach", {"dir": other}, "allow", config.reaches),
            ("xsm_approve", {"id": "r1"}, "allow",
             lambda: [r for r in [paths.read_json(paths.path(workers.APPROVALS, "r1.json"))]
                      if r["status"] != "pending"]),
            ("xsm_grant", {"runtime": "codex", "options": ["full_access"], "reason": "r"},
             "allow once", lambda: grants() + decisions()),
        ]

    def _ask(self, name, args, reply, cwd=None, session=AGENT, init=None):
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": name, "arguments": args}}
        out, _ = self._run(init or self.INIT, call,
                           dict({"jsonrpc": "2.0", "id": "xsm-1"}, **reply),
                           cwd=cwd, session=session)
        return next(m for m in out if m.get("id") == 2)["result"]["content"][0]["text"]

    def test_a_persons_choice_acts_in_all_six(self):
        # What makes the refusals below mean something: the same setup acts
        # when the answer is a person's.
        here = os.path.join(self.tmp, "proj")
        os.makedirs(here, exist_ok=True)
        agent, six = self._six(here)
        for name, args, yes, changed in six:
            with self.subTest(name):
                self._ask(name, args, {"result": {"action": "accept", "content": {"answer": yes}}},
                          session=agent)
                self.assertNotEqual(changed(), [])

    def test_an_answer_from_an_automatic_reviewer_changes_nothing(self):
        here = os.path.join(self.tmp, "proj")
        os.makedirs(here, exist_ok=True)
        agent, six = self._six(here)
        for name, args, yes, changed in six:
            with self.subTest(name):
                text = self._ask(name, args, {"result": {
                    "action": "accept", "content": {"answer": yes},
                    "_meta": {"approvals_reviewer": "auto_review"}}}, session=agent)
                self.assertIn("automatic reviewer", text)
                self.assertEqual(changed(), [])

    def test_a_malformed_meta_is_no_answer_even_with_a_valid_choice(self):
        # A _meta that is there but not an object says nothing about who
        # answered; it was read as no _meta, and all six acted (2026-09-28).
        here = os.path.join(self.tmp, "proj")
        os.makedirs(here, exist_ok=True)
        agent, six = self._six(here)
        for name, args, yes, changed in six:
            for meta in (["invalid"], "auto_review"):
                with self.subTest(name, meta=meta):
                    text = self._ask(name, args, {"result": {
                        "action": "accept", "content": {"answer": yes}, "_meta": meta}},
                        session=agent)
                    self.assertIn("_meta", text)
                    self.assertEqual(changed(), [])

    def test_a_malformed_action_is_a_tool_result_not_a_dead_server(self):
        # action=[] with an automatic reviewer raised TypeError past serve(),
        # so the server exited without replying (2026-09-28).
        here = os.path.join(self.tmp, "proj")
        os.makedirs(here, exist_ok=True)
        agent, six = self._six(here)
        for name, args, yes, changed in six:
            for action in ([], {}, None):
                with self.subTest(name, action=action):
                    text = self._ask(name, args, {"result": {
                        "action": action, "content": {"answer": yes},
                        "_meta": {"approvals_reviewer": "auto_review"}}}, session=agent)
                    self.assertIn("not a form answer", text)
                    self.assertEqual(changed(), [])

    def test_a_malformed_answer_is_no_answer_and_no_crash(self):
        here = os.path.join(self.tmp, "proj")
        os.makedirs(here, exist_ok=True)
        replies = [{"result": {"action": "accept", "content": ["allow"]}},
                   {"result": {"action": "accept", "content": {"answer": None}}},
                   {"result": {"action": "accept", "content": {"answer": "  "}}},
                   {"result": {"action": "accept", "content": {"answer": "maybe"}}},
                   {"result": "accept"},
                   {"result": {"action": "accept", "content": {}, "_meta": ["x"]}},
                   {"error": "boom"}]
        agent, six = self._six(here)
        for name, args, _, changed in six:
            for reply in replies:
                with self.subTest(name, reply=reply):
                    text = self._ask(name, args, reply, session=agent)
                    self.assertNotIn("None", text)
                    self.assertEqual(changed(), [])

    def test_a_join_denied_by_the_person_offers_no_command(self):
        from xsm import config
        deny = {"result": {"action": "accept", "content": {"answer": "deny"}}}
        text = self._ask("xsm_join", {"project": "demo"}, deny)
        self.assertIn("they chose 'deny'", text)
        self.assertNotIn("xsm join", text)
        self.assertNotIn("terminal", text)
        self.assertEqual(config.projects(), [])

    def test_the_shell_command_is_one_the_agent_can_run_and_is_quoted(self):
        """Issue #9: the fallback is run by the agent from its own folder, and
        --dir is taken only from a person at a terminal, so it carries none."""
        import shlex
        from xsm import config
        here = os.path.join(self.tmp, "my proj")
        text = self._ask("xsm_join", {"project": "demo"}, {"result": {"action": "decline"}},
                         cwd=here)
        self.assertIn("`xsm join demo` in your shell", text)
        self.assertNotIn("--dir", text)
        self.assertIn("approval_policy", text)
        other = os.path.join(self.tmp, "other folder")
        os.makedirs(other)
        text = self._ask("xsm_reach", {"dir": other}, {"result": {"action": "cancel"}})
        self.assertIn("`xsm reach %s --session ref:%s`" % (
            shlex.quote(config.project_root(other)), AGENT["ref"]), text)

    def test_the_reach_command_names_the_absolute_root_of_a_relative_dir(self):
        # The person's terminal may be anywhere, so "../other folder" is named
        # as the root the form showed, taken from the asking session's folder.
        import shlex
        from xsm import config
        here = os.path.join(self.tmp, "proj")
        other = os.path.join(self.tmp, "other folder")
        os.makedirs(other)
        command = "`xsm reach %s --session ref:%s`" % (
            shlex.quote(config.project_root(other)), AGENT["ref"])
        text = self._ask("xsm_reach", {"dir": "../other folder"},
                         {"result": {"action": "decline"}}, cwd=here)
        self.assertIn(command, text)
        self.assertNotIn("..", text)
        init = dict(self.INIT, params={"protocolVersion": "2025-06-18", "capabilities": {}})
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "xsm_reach", "arguments": {"dir": "../other folder"}}}
        out, _ = self._run(init, call, cwd=here)
        self.assertIn(command, next(m for m in out if m.get("id") == 2)
                      ["result"]["content"][0]["text"])

    def test_a_bare_decline_names_codex_only_for_codex(self):
        init = lambda name: dict(self.INIT, params=dict(self.INIT["params"],
                                                        clientInfo={"name": name}))
        decline = {"result": {"action": "decline"}}
        text = self._ask("xsm_join", {"project": "demo"}, decline, init=init("codex-mcp-client"))
        self.assertIn("by Codex without showing the form", text)
        text = self._ask("xsm_join", {"project": "demo"}, decline, init=init("claude-code"))
        self.assertIn("by the client without showing the form", text)
        self.assertNotIn("Codex", text)
        text = self._ask("xsm_join", {"project": "demo"}, decline)
        self.assertIn("by the client without showing the form (Codex does this", text)

    def test_decide_offers_options_as_they_are_compared(self):
        # " yes " was offered as is and compared stripped, so the person's
        # choice was refused (2026-09-28).
        from xsm import channel
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "xsm_decide", "arguments": {"question": "Ship?",
                                                "options": [" yes ", "no", "yes", "  "]}}}
        for chosen in ("yes", " yes "):
            with self.subTest(chosen=chosen):
                answer = {"jsonrpc": "2.0", "id": "xsm-1", "result": {
                    "action": "accept", "content": {"answer": chosen}}}
                out, here = self._run(self.INIT, call, answer)
                elicit = next(m for m in out if m.get("method") == "elicitation/create")
                self.assertEqual(elicit["params"]["requestedSchema"]["properties"]["answer"]
                                 ["enum"], ["yes", "no"])
                self.assertIn("recorded decision", next(m for m in out if m.get("id") == 2)
                              ["result"]["content"][0]["text"])
                rec = channel.read(channel.resolve(here)[1])[-1]
                self.assertEqual(rec["approved"]["answer"], "yes")

    def test_a_tool_that_fails_is_that_calls_error_not_the_servers_end(self):
        from xsm import mcp
        msgs = [self.INIT,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "xsm_post", "arguments": {"text": "x"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}]
        out = io.StringIO()
        server = mcp.Server(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        server.session = lambda: dict(AGENT, cwd=self.tmp)

        def boom(*a):
            raise TypeError("unhashable type: 'list'")
        server._call = boom
        self.assertEqual(server.serve(), 0)
        replies = {m["id"]: m for m in map(json.loads, out.getvalue().splitlines())}
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertIn("unhashable", replies[2]["result"]["content"][0]["text"])
        self.assertIn("tools", replies[3]["result"])

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
        text = next(m for m in out if m.get("id") == 2)["result"]["content"][0]["text"]
        self.assertIn("cannot show a form", text)
        self.assertIn("xsm post --tag decision", text)
        self.assertNotIn("in a terminal", text)


if __name__ == "__main__":
    unittest.main()


class McpSendTest(TempState):
    def test_xsm_send_sends_as_the_session(self):
        import json as _json
        from xsm import mcp, registry, send
        seen = []
        send.send = lambda target, text, **kw: seen.append((target, text, kw["sender"]["ref"],
                                                            kw["kind"], kw["reply_to"])) or \
            type("R", (), {"status": "delivered", "reason": "ok"})()
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "xsm_send", "arguments": {"target": "ref:abcdef@peer", "text": "56",
                                                      "kind": "reply", "reply_to": "m1"}}}]
        out = io.StringIO()
        server = mcp.Server(io.StringIO("".join(_json.dumps(m) + "\n" for m in msgs)), out)
        server.session = lambda: dict(AGENT, cwd=self.tmp)
        server.serve()
        self.assertEqual(seen, [("ref:abcdef@peer", "56", "aaaaaa", "reply", "m1")])
