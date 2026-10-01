"""One yes connects two folders and sends the message that was refused for the
lack of it (user decision, 2026-10-01: "응 한 번에 연결하고 재전송까지 되게 해").

The refused send is held (outbox.py) and the connection is asked for through
the ordinary consent machinery, so the person's single reply is the verdict:
refuse and ask, show the reply, then connect and deliver."""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_xsm  # noqa: E402

# Module names only: a class imported here by name would run its tests twice.
TempState = test_xsm.TempState


class _Sessions(TempState):
    """A Claude session in folder A and a Codex session in folder B, two
    repositories nobody linked, and a recorded delivery to B."""

    def setUp(self):
        super().setUp()
        from xsm import adapters, registry, workers
        self.a = os.path.realpath(os.path.join(self.tmp, "proj-a"))
        self.b = os.path.realpath(os.path.join(self.tmp, "proj-b"))
        for folder in (self.a, self.b):
            os.makedirs(folder, exist_ok=True)
        registry.upsert("claude", os.path.join(self.tmp, "homes", "claude"), "s-sender",
                        os.getpid(), self.a, name="sender")
        registry.upsert("codex", os.path.join(self.tmp, "homes", "codex"), "s-target",
                        os.getpid(), self.b, name="target")
        self.me = registry.by_session("claude", "s-sender")
        self.you = registry.by_session("codex", "s-target")
        self.target = "ref:%s" % self.you["ref"]
        self.sent = []
        for patch in (
                mock.patch.object(adapters, "to_codex", lambda *a: self.sent.append(a[2])),
                mock.patch.object(registry, "me", return_value=self.me),
                mock.patch.object(workers, "human_terminal", return_value=False),
                mock.patch.dict(os.environ, {"CODEX_SANDBOX": "", "XSM_SANDBOXED": ""})):
            patch.start()
            self.addCleanup(patch.stop)

    def _cli(self, *argv):
        from xsm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def _send(self, text="hello b", *extra):
        return self._cli("send", self.target, "--text", text, *extra)

    def _reply(self, text):
        from xsm import consent
        consent.record(self.me, {"hook_event_name": "UserPromptSubmit", "prompt": text})

    def _waited(self):
        """The agent read what it was shown: the next run may pass (consent.SHOW_DELAY)."""
        from xsm import consent, paths
        p = consent.pending_files(self.me["ref"])[0]
        entry = paths.read_json(p)
        entry["shown_t"] -= consent.SHOW_DELAY + 1
        paths.write_json(p, entry)

    def _pending(self):
        from xsm import consent, paths
        files = consent.pending_files(self.me["ref"])
        return paths.read_json(files[0]) if files else None

    def _held_id(self, text):
        found = re.search(r"held as ([0-9a-f]{16})", text)
        self.assertIsNotNone(found, text)
        return found.group(1)

    def _held_files(self):
        from xsm import outbox, paths
        folder = paths.path(outbox.OUTBOX)
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


class OneYesTest(_Sessions):
    def test_the_refusal_keeps_the_message_and_asks_for_the_exact_connection(self):
        from xsm import config, consent, ledger, outbox, paths
        code, text = self._send()
        self.assertEqual(code, 2, text)
        self.assertIn("out of scope", text)
        self.assertIn("NOT sent", text)
        self.assertIn("needs your user's yes to connecting", text)
        self.assertIn("First ask them, in plain words", text)
        self.assertIn("run this same command again", text)
        self.assertIn("once more", text)
        self.assertIn("nothing more to ask", text)
        msg_id = self._held_id(text)
        held = paths.read_json(paths.path(outbox.OUTBOX, "%s.json" % msg_id))
        self.assertEqual((held["body"], held["spec"], held["kind"], held["ref"]),
                         ("hello b", self.target, "note", self.me["ref"]))
        entry = self._pending()
        self.assertEqual((entry["verb"], entry["target"], entry["here"], entry["verdict"]),
                         ("link", self.b, self.a, None), "keyed as `xsm link` keys it")
        self.assertEqual(self.sent, [])
        self.assertEqual(config.links(), [])
        self.assertEqual(ledger.recent(), [], "nothing was queued")
        self.assertTrue(oct(os.stat(paths.path(outbox.OUTBOX, "%s.json" % msg_id)).st_mode
                            & 0o777) == "0o600")
        self.assertIn(consent.ASKED, os.listdir(self.tmp))

    def test_one_yes_connects_and_sends_exactly_once(self):
        from xsm import config, ledger, paths
        code, text = self._send()
        msg_id = self._held_id(text)
        self._reply("응")
        code, shown = self._send()
        self.assertEqual(code, 2, shown)
        self.assertIn('your user replied: "응"', shown)
        self.assertIn("NOT sent", shown)
        self.assertEqual((self.sent, config.links()), ([], []), "showing is not going ahead")
        self._waited()
        code, passed = self._send()
        self.assertEqual(code, 3, passed)                  # sent-unconfirmed, as any send
        lines = passed.splitlines()
        self.assertEqual(lines[0], 'approved on your user\'s reply: "응"')
        self.assertTrue(lines[1].startswith("linked: the sessions in "), lines)
        self.assertTrue(lines[-1].startswith("sent-unconfirmed:"), lines)
        self.assertEqual(len(config.links()), 1)
        self.assertIn("응", config.links()[0]["by"])
        self.assertEqual(len(self.sent), 1, "delivered exactly once")
        self.assertIn("hello b", self.sent[0])
        self.assertIn(msg_id, self.sent[0], "under the id it was held with")
        rows = ledger.recent()
        self.assertEqual([r["id"] for r in rows], [msg_id])
        self.assertEqual(self._held_files(), [], "the held copy went with it")
        self.assertIsNone(self._pending(), "the ask is used up")
        logged = [d for d in paths.read_jsonl("decisions.jsonl") if d.get("event") == "consent"]
        self.assertEqual((logged[-1]["verb"], logged[-1]["verdict"]), ("link", "응"))
        # the held id is no longer anything to send
        code, again = self._cli("send", "--held", msg_id)
        self.assertEqual(code, 2, again)
        self.assertIn("no message %s is held" % msg_id, again)
        self.assertEqual(len(self.sent), 1)

    def test_a_no_leaves_it_unsent_and_the_agent_is_told_so(self):
        from xsm import config, ledger
        code, text = self._send()
        msg_id = self._held_id(text)
        self._reply("아니, 연결하지 마")
        code, shown = self._send()
        self.assertEqual(code, 2, shown)
        self.assertIn('your user replied: "아니, 연결하지 마"', shown)
        self.assertIn("if it is a no or a question, do not", shown)
        self.assertIn("NOT sent", shown)
        self.assertEqual((self.sent, config.links(), ledger.recent()), ([], [], []))
        self.assertEqual(self._held_files(), ["%s.json" % msg_id], "kept, in case they change "
                                                                    "their mind")

    def test_running_the_refused_command_again_holds_one_message(self):
        _, first = self._send()
        _, second = self._send()
        self.assertEqual(self._held_id(first), self._held_id(second))
        self.assertEqual(len(self._held_files()), 1)
        _, other = self._send("a different text")
        self.assertNotEqual(self._held_id(other), self._held_id(first))
        self.assertEqual(len(self._held_files()), 2)

    def test_held_flag_sends_the_kept_message_and_needs_no_text(self):
        from xsm import config
        _, text = self._send("the kept text")
        msg_id = self._held_id(text)
        code, said = self._cli("send", "--held", msg_id)
        self.assertEqual(code, 2, said)
        self.assertIn("run this same command again", said)
        self._reply("응")
        self.assertIn('your user replied: "응"', self._cli("send", "--held", msg_id)[1])
        self._waited()
        code, passed = self._cli("send", "--held", msg_id)
        self.assertEqual(code, 3, passed)
        self.assertEqual(len(config.links()), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("the kept text", self.sent[0])

    def test_held_takes_no_other_arguments(self):
        code, text = self._cli("send", "--held", "abc", "--text", "x")
        self.assertEqual(code, 4, text)
        code, text = self._cli("send")
        self.assertEqual(code, 4, text)

    def test_only_the_session_that_held_it_can_send_it(self):
        _, text = self._send()
        msg_id = self._held_id(text)
        from xsm import registry
        other = dict(self.me, ref="eeeeee", session_id="s-other")
        with mock.patch.object(registry, "me", return_value=other):
            code, said = self._cli("send", "--held", msg_id)
        self.assertEqual(code, 2, said)
        self.assertIn("no message %s is held for this session" % msg_id, said)

    def test_a_held_message_that_is_in_scope_by_then_goes_without_asking(self):
        from xsm import config
        _, text = self._send()
        msg_id = self._held_id(text)
        config.add_link(self.a, self.b, "a person at a terminal")
        code, passed = self._cli("send", "--held", msg_id)
        self.assertEqual(code, 3, passed)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self._held_files(), [])

    def test_an_expired_held_message_is_gone_and_pruned(self):
        import time
        from xsm import outbox, paths
        _, text = self._send()
        msg_id = self._held_id(text)
        p = paths.path(outbox.OUTBOX, "%s.json" % msg_id)
        rec = paths.read_json(p)
        rec["t"] = time.time() - outbox.TTL - 60
        paths.write_json(p, rec)
        code, said = self._cli("send", "--held", msg_id)
        self.assertIn("no message %s is held" % msg_id, said)
        self.assertEqual(outbox.prune(dry_run=True), [msg_id])
        self.assertEqual(outbox.prune(), [msg_id])
        self.assertEqual(self._held_files(), [])

    def test_the_json_result_carries_the_id_and_the_notes(self):
        _, text = self._cli("send", self.target, "--text", "hello b", "--json")
        said = json.loads(text)
        self.assertEqual(said["status"], "refused")
        self.assertTrue(re.fullmatch(r"[0-9a-f]{16}", said["id"]))
        self._reply("응")
        self._cli("send", self.target, "--text", "hello b")
        self._waited()
        _, text = self._cli("send", self.target, "--text", "hello b", "--json")
        done = json.loads(text)
        self.assertEqual(done["status"], "sent-unconfirmed")
        self.assertEqual(done["id"], said["id"])
        self.assertEqual(len(done["notes"]), 2)


class FailOpenTest(_Sessions):
    def test_a_session_that_cannot_keep_a_reply_is_refused_as_before(self):
        from xsm import config
        with mock.patch.dict(os.environ, {"XSM_WORKER": "w1"}):
            code, text = self._send()
        self.assertEqual(code, 2, text)
        self.assertIn("out of scope", text)
        self.assertIn("xsm link", text, "today's advice")
        self.assertNotIn("held as", text, "nothing is promised that cannot be kept")
        self.assertEqual(self._held_files(), [])
        self.assertEqual(config.links(), [])

    def test_a_person_at_a_terminal_gets_the_refusal_not_a_held_message(self):
        from xsm import workers
        with mock.patch.object(workers, "human_terminal", return_value=True):
            code, text = self._send()
        self.assertIn("xsm link", text)
        self.assertEqual(self._held_files(), [])

    def test_a_message_that_cannot_be_kept_is_refused_as_before(self):
        from xsm import outbox
        with mock.patch.object(outbox, "put", return_value=None):
            code, text = self._send()
        self.assertEqual(code, 2, text)
        self.assertIn("xsm link", text)
        self.assertNotIn("held as", text)

    def test_a_connection_that_cannot_be_made_keeps_the_message_and_says_so(self):
        from xsm import config
        _, text = self._send()
        msg_id = self._held_id(text)
        self._reply("응")
        self._send()
        self._waited()
        with mock.patch.object(config, "add_link", side_effect=ValueError("not a folder")):
            code, said = self._send()
        self.assertEqual(code, 2, said)
        self.assertIn("could not be made: not a folder", said)
        self.assertIn(msg_id, said)
        self.assertEqual((self.sent, self._held_files()), ([], ["%s.json" % msg_id]))

    def test_a_remote_target_is_not_held(self):
        code, text = self._cli("send", "box:somebody", "--text", "hi")
        self.assertNotIn("held as", text)
        self.assertEqual(self._held_files(), [])


class PlanTest(_Sessions):
    def test_a_link_is_the_usual_connection(self):
        from xsm import connect
        plan = connect.plan_for(self.me, self.you)
        self.assertEqual((plan["verb"], plan["target"], plan["here"]), ("link", self.b, self.a))

    def test_a_target_in_a_named_project_the_sender_has_not_joined_is_a_join(self):
        from xsm import config, connect
        config.join("demo", self.b)
        plan = connect.plan_for(self.me, self.you)
        self.assertEqual((plan["verb"], plan["target"], plan["here"]), ("join", "demo", None))
        config.join("second", self.b)
        self.assertEqual(connect.plan_for(self.me, self.you)["verb"], "link",
                         "two projects to pick from: the link names no project")

    def test_a_folder_that_would_open_every_session_asks_for_a_reach_instead(self):
        from xsm import connect
        broad = dict(self.me, cwd=os.path.expanduser("~"))
        plan = connect.plan_for(broad, self.you)
        self.assertEqual((plan["verb"], plan["target"], plan["here"]), ("reach", self.b, None))
        self.assertIn("only for as long as it runs", plan["what"])
        under = dict(self.you, cwd="/")
        self.assertEqual(connect.plan_for(self.me, under)["verb"], "reach")

    def test_no_plan_without_two_folders(self):
        from xsm import connect
        self.assertIsNone(connect.plan_for(dict(self.me, cwd=None), self.you))
        self.assertIsNone(connect.plan_for(self.me, dict(self.you, cwd=self.a)),
                          "the same project already")
        self.assertIsNone(connect.plan_for(dict(self.me, ref=None), self.you))

    def _one_yes(self, sender, text):
        """The three runs of one send, by `sender`: ask, show the reply, connect
        and send. The last result."""
        from xsm import consent, paths, send
        first = send.send(self.target, text, sender=sender)
        self.assertEqual(first.status, "refused")
        self.assertIn("NOT sent", first.reason)
        consent.record(sender, {"hook_event_name": "UserPromptSubmit", "prompt": "응"})
        shown = send.send(self.target, text, sender=sender)
        self.assertIn('your user replied: "응"', shown.reason)
        p = consent.pending_files(sender["ref"])[0]
        entry = paths.read_json(p)
        entry["shown_t"] -= consent.SHOW_DELAY + 1
        paths.write_json(p, entry)
        return send.send(self.target, text, sender=sender)

    def test_a_join_connects_and_sends_on_one_yes(self):
        from xsm import config
        config.join("demo", self.b)
        done = self._one_yes(self.me, "hi join")
        self.assertEqual(done.status, "sent-unconfirmed", done.reason)
        self.assertEqual(len(done.notes), 2)
        self.assertTrue(done.notes[1].startswith("joined project demo:"), done.notes)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(self.a, [m["root"] for p in config.projects() for m in p["members"]])

    def test_a_reach_connects_and_sends_on_one_yes(self):
        from xsm import config
        broad = dict(self.me, cwd=os.path.expanduser("~"), state="live")
        done = self._one_yes(broad, "hi reach")
        self.assertEqual(done.status, "sent-unconfirmed", done.reason)
        self.assertTrue(done.notes[1].startswith("allowed:"), done.notes)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([r["ref"] for r in config.reaches()], [self.me["ref"]])
        self.assertEqual(config.links(), [], "no folder was linked")


class McpTest(_Sessions):
    def _tool(self, args, name="xsm_send"):
        from xsm import mcp
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": name, "arguments": args}}]
        out = io.StringIO()
        server = mcp.Server(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        server.session = lambda: dict(self.me)
        server.serve()
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        return next(m for m in replies if m.get("id") == 2)["result"]["content"][0]["text"]

    def test_the_mcp_tool_holds_shows_and_connects_the_same_way(self):
        from xsm import config
        args = {"target": self.target, "text": "hello from mcp", "wait": 0}
        first = self._tool(args)
        self.assertTrue(first.startswith("refused: out of scope"), first)
        self.assertIn("NOT sent", first)
        msg_id = self._held_id(first)
        self._reply("응")
        self.assertIn('your user replied: "응"', self._tool(args))
        self.assertEqual((self.sent, config.links()), ([], []))
        self._waited()
        done = self._tool(args).splitlines()
        self.assertEqual(done[0], 'approved on your user\'s reply: "응"', done)
        self.assertTrue(done[1].startswith("linked:"), done)
        self.assertTrue(done[-1].startswith("sent-unconfirmed:"), done)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("hello from mcp", self.sent[0])
        self.assertEqual(self._held_files(), [])
        self.assertIn("no message %s is held" % msg_id, self._tool({"held": msg_id}))

    def test_the_mcp_tool_sends_a_held_message_by_its_id(self):
        first = self._tool({"target": self.target, "text": "by id", "wait": 0})
        msg_id = self._held_id(first)
        self.assertIn("run this same command again", self._tool({"held": msg_id}))
        self._reply("응")
        self._tool({"held": msg_id})
        self._waited()
        self.assertTrue(self._tool({"held": msg_id}).splitlines()[-1].startswith(
            "sent-unconfirmed:"))
        self.assertEqual(len(self.sent), 1)


class NativeBounceTest(_Sessions):
    """A Claude SendMessage held out of scope: the sender's note leads to the
    same one yes (the receiving hook holds the text and asks in the sender's
    session)."""

    def _bounce(self, text="native hello"):
        from xsm import bounce
        bounce.record(self.me, self.you, "out of scope: different repositories", "heldfile",
                      text, self.b)
        return bounce.notice(self.me["session_id"])

    def test_the_note_names_the_one_command_and_the_ask_is_recorded(self):
        from xsm import config
        note = self._bounce()
        msg_id = re.search(r"kept as ([0-9a-f]{16})", note).group(1)
        self.assertIn("NOT delivered", note)
        self.assertIn("xsm send --held %s" % msg_id, note)
        self.assertIn("xsm_send tool with held=%s" % msg_id, note)
        self.assertIn("ask them once", note)
        self.assertIn("nothing more to ask", note)
        self.assertIn("Do not report the message as delivered", note)
        entry = self._pending()
        self.assertEqual((entry["verb"], entry["target"], entry["here"], entry["verdict"]),
                         ("link", self.b, self.a, None))
        self.assertEqual(config.links(), [])
        self.assertEqual(self._held_files(), ["%s.json" % msg_id])

    def test_one_yes_then_sends_the_native_message_as_an_xsm_note(self):
        from xsm import config
        msg_id = re.search(r"kept as ([0-9a-f]{16})", self._bounce("see the diff")).group(1)
        self.assertIn("run this same command again", self._cli("send", "--held", msg_id)[1])
        self._reply("응")
        self.assertIn('your user replied: "응"', self._cli("send", "--held", msg_id)[1])
        self._waited()
        code, passed = self._cli("send", "--held", msg_id)
        self.assertEqual(code, 3, passed)
        self.assertEqual(len(config.links()), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("see the diff", self.sent[0])

    def test_a_note_that_cannot_hold_the_text_is_the_note_it_was(self):
        from xsm import connect
        with mock.patch.object(connect, "hold_native", side_effect=OSError("disk")):
            note = self._bounce()
        self.assertIn("NOT delivered", note)
        self.assertIn("xsm link %s" % self.b, note, "today's advice")
        self.assertNotIn("--held", note)

    def test_a_receiver_that_cannot_keep_the_ask_leaves_the_note_as_it_was(self):
        with mock.patch.dict(os.environ, {"XSM_WORKER": "w1"}):
            note = self._bounce()
        self.assertIn("xsm link %s" % self.b, note)
        self.assertNotIn("--held", note)
        self.assertEqual(self._held_files(), [])


class HousekeepingTest(_Sessions):
    def test_the_prune_summary_counts_held_sends(self):
        self._send()
        code, text = self._cli("prune", "--dry-run")
        self.assertEqual(code, 0, text)
        self.assertIn("0 held send(s)", text)


if __name__ == "__main__":
    unittest.main()
