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

    def _waited(self):
        """The agent reads what it was shown before it runs the command again:
        the second run passes no sooner than consent.SHOW_DELAY after the first."""
        from xsm import consent, paths
        p = consent._pending_path(self.me["ref"])
        entry = paths.read_json(p)
        entry["shown_t"] = entry["shown_t"] - consent.SHOW_DELAY - 1
        paths.write_json(p, entry)

    def _shows(self, argv, reply):
        """The first run after a reply shows it and does not go ahead (2026-10-01)."""
        code, text = self._cli(argv)
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "%s"' % reply, text)
        self._waited()
        return text

    def _answered(self, answers, annotations=None, me=None, tool="AskUserQuestion",
                  event="PostToolUse", response=None):
        """The hook input Claude Code gives for an answered AskUserQuestion. The
        tool_response is the tool's own result, as a transcript records it
        (toolUseResult): questions, answers by question text, annotations."""
        from xsm import consent
        questions = [{"question": q, "header": "Q", "multiSelect": False,
                      "options": [{"label": "Yes", "description": "go"}]} for q in answers]
        consent.record(me or self.me, {
            "hook_event_name": event, "tool_name": tool, "session_id": "s-agent",
            "tool_input": {"questions": questions},
            "tool_response": response if response is not None else {
                "questions": questions, "answers": answers, "annotations": annotations or {}}})

    def _pending(self):
        from xsm import consent, paths
        return paths.read_json(consent._pending_path(self.me["ref"]))

    def _asked_files(self):
        folder = os.path.join(self.tmp, "asked")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

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
        self._shows(["approve", "r1"], "응 허용")
        self.assertEqual((paths.read_json(workers._approval_path("r1")) or {})["status"], "pending")
        code, text = self._cli(["approve", "r1"])
        self.assertEqual(code, 0, text)
        done = paths.read_json(workers._approval_path("r1")) or {}
        self.assertEqual(done["status"], "approved")
        self.assertIn("응 허용", done["answered_by"])

    def test_a_worker_no_session_started_is_not_anothers(self):
        from xsm import paths, workers
        workers.save({"name": "w9", "runtime": "claude", "mode": "background", "pane": "%9",
                      "cwd": self.tmp, "created": 0})
        paths.write_json(workers._approval_path("r9"), {"id": "r9", "worker": "w9",
                                                        "summary": "x", "status": "pending"})
        code, text = self._cli(["approve", "r9"])
        self.assertEqual(code, 2, text)
        self.assertIn("no session started", text)
        with self.assertRaises(workers.WorkerError) as cm:
            workers.answer_asked("r9", True, "abc123")
        self.assertIn("no session started", str(cm.exception))

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
        with self.assertRaises(workers.WorkerError) as cm:
            workers.use_grant(None, self.me, "codex", self.tmp, ["full_access"])
        self.assertIn('your user replied: "yes, full access is fine this once"',
                      str(cm.exception), "shown first, not acted on")
        self._waited()
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
        self._shows(["attempts", "clear", key], "ok clear it")
        self.assertTrue(attempts.read(key))
        self.assertEqual(self._cli(["attempts", "clear", key])[0], 0)
        self.assertFalse(attempts.read(key))

    def test_letting_xsm_start_workers_inside_a_framework(self):
        from xsm import config
        self._asks(["frameworks", "ignore", "orca"])
        self._reply("그래")
        self._shows(["frameworks", "ignore", "orca"], "그래")
        self.assertNotIn("orca", config.ignored_frameworks())
        self.assertEqual(self._cli(["frameworks", "ignore", "orca"])[0], 0)
        self.assertIn("orca", config.ignored_frameworks())

    def test_lifting_a_block(self):
        from xsm import config
        config.block("dddddd")
        self._asks(["unblock", "dddddd"])
        self._reply("unblock it")
        self._shows(["unblock", "dddddd"], "unblock it")
        self.assertIn("dddddd", config.blocked())
        self.assertEqual(self._cli(["unblock", "dddddd"])[0], 0)
        self.assertNotIn("dddddd", config.blocked())

    def test_a_decision_is_the_persons_words(self):
        from xsm import channel
        self._asks(["post", "--tag", "decision", "ship on friday"])
        self._reply("yes, friday")
        self._shows(["post", "--tag", "decision", "ship on friday"], "yes, friday")
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
        self._shows(argv, "맞아, 보증해")
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
        """User decision, 2026-10-01: the agent asks and runs the command; no
        text of ours tells a person to go and type one. Every phrase that did
        is named here; a place where a person at a terminal really is the
        subject is listed below, explicitly."""
        import re
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        readmes = ["README.md", "README_ko.md"]
        files = [os.path.join("xsm", n) for n in sorted(os.listdir(os.path.join(repo, "xsm")))
                 if n.endswith(".py")] + [
            os.path.join("skills", "xsm", "SKILL.md"),
            os.path.join("skills", "xsm", "references", "guide.md")] + readmes
        phrases = ("in a terminal", "needs a terminal", "only a person can", "a person clears",
                   "(a person only)", "a person only", "needs a person", "a person can lift",
                   "they can type", "you can type", "ask them to type", "run /xsm",
                   "run: /xsm", "with: /xsm", "with `/xsm", "type /xsm", "types /xsm",
                   "run $xsm", "type $xsm")
        # Outside the READMEs, which may say what a person can type themselves
        # once the primary way (ask the agent) is named, the backtick forms too,
        # and where a person at a terminal is the subject at all (2026-10-01).
        wider = ("type `/xsm", "types `/xsm", "typing `/xsm", "type `$xsm", "types `$xsm",
                 "typing `$xsm", "at a terminal")
        # (file, text around the phrase): a person typing at a terminal is the point.
        genuine = (("xsm/otlp_export.py", "A person runs this in a terminal"),
                   ("README.md", "The same commands work in a terminal as"),
                   ("xsm/install.py", "told the person to type /xsm link instead"),   # history
                   ("skills/xsm/references/guide.md", "Never ask them to type shell commands"),
                   # `--dir` speaks for another folder: only a person at a terminal, and
                   # the refusal says so (the agent runs from its own folder).
                   ("xsm/cli.py", "only a person at a terminal may use it with"),
                   ("xsm/mcp.py", "--dir is taken only from a person at a terminal"),
                   # Who can answer for a worker no session started, and who wrote a record.
                   ("xsm/cli.py", "so only a person at a terminal can answer"),
                   ("xsm/channel.py", "not a person at a terminal and not a registered"),
                   ("xsm/doc.py", "a person at a terminal, or a session asking its user"),
                   ("xsm/workers.py", "A person typing `spawn` at a terminal needs no grant"),
                   ("xsm/workers.py", "Approving is a person's: at a terminal, or through"))
        for name in files:
            with open(os.path.join(repo, name), encoding="utf-8") as fh:
                text = fh.read()
            # Adjacent string literals and wrapped lines read as one line.
            text = re.sub(r'"\s*\n\s*"', "", text)
            text = re.sub(r"\s*\n\s*(#\s*)?", " ", text)
            for phrase in phrases + (() if name in readmes else wider):
                for m in re.finditer(re.escape(phrase), text):
                    around = text[max(0, m.start() - 60):m.end() + 60]
                    if any(name.replace(os.sep, "/") == f and ok in around for f, ok in genuine):
                        continue
                    self.fail("%s still says %r: ...%s..." % (name, phrase, around))

    def test_a_task_notification_is_never_the_reply(self):
        """Measured 2026-10-01: a background task's completion arrived through
        UserPromptSubmit and was stored as the verdict."""
        self._asks(["unblock", "ffffff"])
        for text in ("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
                     "</task-notification>",
                     "<system-reminder>Hook says hi</system-reminder>",
                     "<command-name>/clear</command-name>\n<command-message>clear</command-message>",
                     "<local-command-stdout>ok</local-command-stdout>",
                     "<bash-input>ls</bash-input>",
                     "<user-prompt-submit-hook>x</user-prompt-submit-hook>",
                     "<some-new-tag attr=\"1\">anything</some-new-tag>"):
            with self.subTest(text=text[:30]):
                self._reply(text)
                self._asks(["unblock", "ffffff"])
        self._reply("<br-tag> 이건 닫는 태그가 없는 사람의 말")
        self._shows(["unblock", "ffffff"], "<br-tag> 이건 닫는 태그가 없는 사람의 말")

    def test_a_slash_command_is_not_the_reply(self):
        self._asks(["unblock", "aaaaaa"])
        for text in ("/clear", "/model opus", "/compact  keep the tests", "/xsm list"):
            with self.subTest(text=text):
                self._reply(text)
                self._asks(["unblock", "aaaaaa"])
        self._reply("/tmp/x 에서 풀어도 돼")            # a path is a person talking
        self._shows(["unblock", "aaaaaa"], "/tmp/x 에서 풀어도 돼")

    def test_the_latest_reply_is_the_verdict(self):
        """A clarifying question first, the yes after it: the yes decides, and
        a no after a yes does too."""
        from xsm import config
        config.block("bbbbbb")
        self._asks(["unblock", "bbbbbb"])
        self._reply("이거 풀면 다른 기록도 같이 풀려?")
        self._reply("응")
        self._shows(["unblock", "bbbbbb"], "응")
        self.assertEqual(self._cli(["unblock", "bbbbbb"])[0], 0)
        self.assertNotIn("bbbbbb", config.blocked())

    def test_a_reply_is_shown_before_it_can_pass(self):
        from xsm import config
        config.block("cccccc")
        self._asks(["unblock", "cccccc"])
        self._reply("아니 풀지 마")
        self._shows(["unblock", "cccccc"], "아니 풀지 마")
        self.assertIn("cccccc", config.blocked(), "showing is not going ahead")
        self.assertEqual(self._cli(["unblock", "cccccc"])[0], 0, "the next run goes ahead")

    def test_a_new_reply_after_it_was_shown_must_be_shown_too(self):
        from xsm import config
        config.block("dddddd")
        self._asks(["unblock", "dddddd"])
        self._reply("응")
        self._shows(["unblock", "dddddd"], "응")
        self._reply("아 잠깐, 아니야 풀지 마")
        self._shows(["unblock", "dddddd"], "아 잠깐, 아니야 풀지 마")
        self.assertIn("dddddd", config.blocked())
        self.assertEqual(self._cli(["unblock", "dddddd"])[0], 0)
        self.assertNotIn("dddddd", config.blocked())

    def test_a_verdict_is_used_once(self):
        from xsm import config
        config.block("eeeeee")
        self._asks(["unblock", "eeeeee"])
        self._reply("응")
        self._shows(["unblock", "eeeeee"], "응")
        self.assertEqual(self._cli(["unblock", "eeeeee"])[0], 0)
        config.block("eeeeee")
        self._asks(["unblock", "eeeeee"])
        self.assertIn("eeeeee", config.blocked())

    def test_the_window_counts_from_the_reply_but_an_unanswered_ask_still_expires(self):
        import time
        from xsm import consent, paths
        self._asks(["unblock", "abcabc"])
        pending = consent._pending_path(self.me["ref"])
        entry = paths.read_json(pending)
        entry["t"] = time.time() - consent.TTL - 60            # asked long ago, unanswered
        paths.write_json(pending, entry)
        self._reply("응")
        self.assertFalse(os.path.exists(pending), "an expired ask keeps nothing, not even a file")
        entry["t"] = time.time() - consent.TTL + 5             # asked just inside the window
        paths.write_json(pending, entry)
        self._reply("응")
        entry = paths.read_json(pending)
        self.assertEqual(entry["verdict"], "응")
        entry["t"] = time.time() - consent.TTL - 60            # the reply is what is counted
        entry["verdict_t"] = time.time() - 5
        paths.write_json(pending, entry)
        self._shows(["unblock", "abcabc"], "응")
        entry = paths.read_json(pending)
        entry["verdict_t"] = time.time() - consent.TTL - 5     # a reply this old is stale too
        paths.write_json(pending, entry)
        self._asks(["unblock", "abcabc"])

    def test_an_ask_is_gone_thirty_minutes_after_it_was_made_however_much_was_said(self):
        """The window slid with every message, so an ask stayed alive for hours
        while the person kept chatting and an unrelated message became its
        verdict (adversarial check, 2026-10-01)."""
        import time
        from xsm import config, consent, paths
        config.block("abc999")
        self._asks(["unblock", "abc999"])
        self._reply("응")
        pending = consent._pending_path(self.me["ref"])
        entry = paths.read_json(pending)
        entry["t"] = time.time() - consent.ASK_MAX + 60           # nearly half an hour old
        entry["verdict_t"] = time.time() - 5
        paths.write_json(pending, entry)
        self._reply("응 풀어")
        self.assertEqual(paths.read_json(pending)["verdict"], "응 풀어", "still inside the cap")
        entry = paths.read_json(pending)
        entry["t"] = time.time() - consent.ASK_MAX - 60           # past it
        entry["verdict_t"] = time.time() - 5                      # the last reply is fresh
        paths.write_json(pending, entry)
        self._reply("그건 그렇고 다른 얘긴데")
        self.assertFalse(os.path.exists(pending), "gone, with the words it kept")
        self._asks(["unblock", "abc999"])                         # a new ask, not the old yes
        self.assertIn("abc999", config.blocked())
        self.assertIsNone(paths.read_json(pending)["verdict"])

    def test_the_rerun_that_passes_comes_after_the_one_that_shows(self):
        """`xsm unblock X || xsm unblock X` showed the reply and used it in the
        same breath, before the agent could read it (2026-10-01)."""
        from xsm import config, consent, paths
        config.block("abc888")
        self._asks(["unblock", "abc888"])
        self._reply("응")
        self.assertEqual(consent.SHOW_DELAY, 1.0)
        code, text = self._cli(["unblock", "abc888"])
        self.assertEqual(code, 2, text)
        code, text = self._cli(["unblock", "abc888"])            # the same line, a moment later
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "응"', text, "shown again, not passed")
        self.assertIn("abc888", config.blocked())
        self._waited()
        self.assertEqual(self._cli(["unblock", "abc888"])[0], 0)
        self.assertNotIn("abc888", config.blocked())

    def test_a_pending_file_from_before_the_delay_still_passes(self):
        """A reply c4852bc already showed has no shown_t."""
        from xsm import config, consent, paths
        config.block("abc777")
        self._asks(["unblock", "abc777"])
        self._reply("응")
        pending = consent._pending_path(self.me["ref"])
        entry = paths.read_json(pending)
        entry.update({"shown": True})
        entry.pop("shown_t", None)
        paths.write_json(pending, entry)
        self.assertEqual(self._cli(["unblock", "abc777"])[0], 0)

    def test_a_reply_written_during_a_show_is_not_lost(self):
        """take_verdict read the old reply, the hook wrote the new one, and the
        show wrote the old one back over it: the old yes would then pass
        (measured 323 of 400, 2026-10-01). The read and the write are one step."""
        import threading
        from xsm import consent, paths
        self._asks(["unblock", "abc666"])
        self._reply("응")
        pending = consent._pending_path(self.me["ref"])
        late = threading.Thread(target=self._reply, args=("아니 잠깐",))
        real, state = paths.write_json, {"first": True}

        def write(p, data, *args, **kwargs):
            if state["first"] and p == pending:
                state["first"] = False
                late.start()
                late.join(0.5)          # unlocked, it is done by now; locked, it waits
            return real(p, data, *args, **kwargs)

        with mock.patch.object(paths, "write_json", write):
            self.assertEqual(consent.take_verdict(self.me, "unblock", "abc666"), ("응", False))
        late.join(10)
        entry = paths.read_json(pending)
        self.assertEqual(entry["verdict"], "아니 잠깐")
        self.assertFalse(entry["shown"], "the new reply has not been shown yet")

    def test_runs_at_the_same_moment_do_not_pass_on_each_others_showing(self):
        import threading
        from xsm import consent, paths
        self._asks(["unblock", "abc555"])
        self._reply("응")
        results, gate = [], threading.Barrier(8)

        def rerun():
            gate.wait()
            results.append(consent.take_verdict(self.me, "unblock", "abc555"))

        threads = [threading.Thread(target=rerun) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results, [("응", False)] * 8, "shown by all, passed by none")
        self.assertTrue(os.path.exists(consent._pending_path(self.me["ref"])))
        entry = paths.read_json(consent._pending_path(self.me["ref"]))
        entry["shown_t"] -= consent.SHOW_DELAY + 1               # read, a moment later
        paths.write_json(consent._pending_path(self.me["ref"]), entry)
        results.clear()
        gate = threading.Barrier(8)
        threads = [threading.Thread(target=rerun) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results.count(("응", True)), 1, "one passes, once")
        self.assertEqual(results.count((None, False)), 7, "the rest find it used up")

    def test_a_prompt_with_no_ask_leaves_nothing_behind(self):
        """Every prompt of every session comes through note_verdict; only an ask
        is worth a lock file."""
        from xsm import consent
        self._reply("hello")
        self.assertEqual(consent.take_verdict(self.me, "unblock", "abc333"), (None, False))
        asked = os.path.join(self.tmp, consent.ASKED)
        self.assertEqual(os.listdir(asked) if os.path.isdir(asked) else [], [])

    def test_a_ref_prefix_names_the_same_session(self):
        """`xsm block ref:abcdef` stored "ref:abcdef" and the gate compares bare
        refs, so nothing was blocked; `unblock` asked about the prefixed text."""
        from xsm import config
        code, text = self._cli(["block", "ref:abcdef"])
        self.assertEqual(code, 0, text)
        self.assertEqual(config.blocked(), {"abcdef"})
        self.assertIn("blocked abcdef", text)
        asked = self._asks(["unblock", "ref:abcdef"])
        self.assertIn("session abcdef", asked)
        self._reply("응")
        self._shows(["unblock", "abcdef"], "응")                  # the bare ref is the same ask
        code, text = self._cli(["unblock", "ref:abcdef"])
        self.assertEqual(code, 0, text)
        self.assertEqual(config.blocked(), set())

    def test_an_empty_or_malformed_ref_is_a_usage_error_before_anyone_is_asked(self):
        """`xsm block ref:` stored an empty string in the deny list, and
        `unblock ref:` asked about nothing and answered "no change for "."""
        from xsm import config
        for argv in (["block", "ref:"], ["block", ""], ["block", "ref: "], ["block", "  "],
                     ["unblock", "ref:"], ["unblock", "a b"], ["block", "ref:a b"]):
            with self.subTest(argv=argv):
                code, text = self._cli(argv)
                self.assertEqual(code, 4, text)
                self.assertIn("usage: xsm %s <ref>" % argv[0], text)
        self.assertEqual(config.blocked(), set())
        self.assertEqual(config.load().get("deny") or [], [])
        self.assertEqual(self._asked_files(), [], "nobody was asked")
        self.assertEqual(self._cli(["block", "ref:abcdef"])[0], 0, "a ref still works")

    def test_the_agent_is_told_to_ask_before_it_reruns(self):
        """The refusal left the order implicit and agents reran before asking
        (2026-10-01): ask, wait, rerun to see the reply, rerun once more on a yes."""
        from xsm import workers
        text = self._asks(["unblock", "abc444"])
        with self.assertRaises(workers.WorkerError) as cm:
            workers.use_grant(None, self.me, "codex", self.tmp, ["full_access"])
        for said in (text, str(cm.exception)):
            order = [said.index(w) for w in ("First ask them", "wait for their answer",
                                             "After they answer, run this same command again",
                                             "shows you that reply", "once more to go ahead")]
            self.assertEqual(order, sorted(order), said)
        self.assertIn("xsm_grant MCP tool", str(cm.exception))

    def test_when_a_reply_cannot_be_kept_the_wording_follows_the_case(self):
        from xsm import consent
        with_tool = consent.cannot_keep("Joining a project", "xsm_join")
        self.assertIn("If the xsm_join MCP tool is available, use it: it asks them with a form. "
                      "If it is not, tell them plainly that this cannot be decided from here.",
                      with_tool)
        without = consent.cannot_keep("Lifting a block", "no")
        self.assertIn("There is no form tool for this decision, so tell them plainly that it "
                      "cannot be decided from here.", without)
        for text in (with_tool, without):
            self.assertNotIn("Otherwise", text)
            self.assertNotIn("type", text)

    def test_only_the_session_that_started_the_worker_approves_on_a_reply(self):
        from xsm import paths, workers
        workers.save({"name": "w3", "runtime": "claude", "mode": "background", "pane": "%3",
                      "cwd": self.tmp, "created": 0, "parent_ref": "someone"})
        paths.write_json(workers._approval_path("r3"), {
            "id": "r3", "worker": "w3", "summary": "Bash: ls", "status": "pending", "t": 0})
        code, text = self._cli(["approve", "r3"])
        self.assertEqual(code, 2)
        self.assertIn("another session started", text)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "asked", "abc123.pending.json")),
                         "no ask for what this session may not do")
        self.assertEqual((paths.read_json(workers._approval_path("r3")) or {})["status"],
                         "pending")

    def test_a_reply_that_cannot_be_kept_is_not_promised(self):
        """No registered session, a worker, or a state folder that cannot be
        written: say so, point at the form tool, never at a keyboard."""
        from xsm import consent
        for label, me, env in (("no session", None, {}),
                               ("a worker", self.me, {"XSM_WORKER": "w1"})):
            with self.subTest(label), mock.patch.dict(os.environ, env):
                self.me = me
                code, text = self._cli(["join", "demo"])
                self.assertEqual(code, 2, text)
                self.assertIn("cannot keep their reply", text)
                self.assertIn("xsm_join MCP tool", text)
                self.assertNotIn("is kept as the verdict", text)
                self.assertNotIn("type", text)

    def test_a_state_folder_that_cannot_be_written_does_not_raise(self):
        from xsm import consent, paths
        with mock.patch.object(paths, "write_json", side_effect=PermissionError("denied")):
            self.assertFalse(consent.take_or_request(self.me, "unblock", "abcabc")[2])
        self.assertTrue(consent.take_or_request(self.me, "unblock", "abcabc")[2])
        with mock.patch.object(paths, "write_json", side_effect=PermissionError("denied")):
            code, text = self._cli(["unblock", "abcabc"])
        self.assertEqual(code, 2, text)
        self.assertIn("cannot keep their reply", text)

    def test_a_spawn_that_cannot_keep_the_reply_says_so(self):
        from xsm import workers
        with self.assertRaises(workers.WorkerError) as cm:
            workers.use_grant(None, None, "codex", self.tmp, ["full_access"])
        self.assertIn("cannot keep their reply", str(cm.exception))
        self.assertIn("xsm_grant MCP tool", str(cm.exception))

    # -- a read-only state folder (adversarial check, 2026-10-01) ---------------------

    def test_a_folder_that_cannot_be_written_refuses_at_the_show_and_the_pass(self):
        """The show step wrote its mark and raised PermissionError: a traceback
        and exit 1 where the other steps said they cannot keep the reply."""
        from xsm import consent, paths
        for step in ("show", "pass"):
            with self.subTest(step):
                self._asks(["unblock", "abc111"])
                self._reply("응")
                if step == "pass":
                    self._shows(["unblock", "abc111"], "응")
                real_unlink = os.unlink

                def unlink(name, *args, **kwargs):
                    if str(name).endswith(".pending.json"):
                        raise PermissionError("denied")
                    return real_unlink(name, *args, **kwargs)

                with mock.patch.object(paths, "write_json",
                                       side_effect=PermissionError("denied")), \
                        mock.patch.object(os, "unlink", unlink):
                    code, text = self._cli(["unblock", "abc111"])
                self.assertEqual(code, 2, text)
                self.assertIn("cannot keep their reply", text)
                self.assertNotIn("Traceback", text)
                os.unlink(consent._pending_path(self.me["ref"]))

    def test_a_read_only_state_folder_is_a_refusal_not_a_crash(self):
        import stat
        from xsm import consent, paths
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        self._asks(["unblock", "abc112"])
        self._reply("응")
        folder = os.path.join(self.tmp, consent.ASKED)
        os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR)
        try:
            code, text = self._cli(["unblock", "abc112"])             # the show
            self.assertEqual(code, 2, text)
            self.assertIn("cannot keep their reply", text)
            entry = paths.read_json(consent._pending_path(self.me["ref"]))
            entry.update({"shown": True, "shown_t": 0})
            os.chmod(folder, stat.S_IRWXU)
            paths.write_json(consent._pending_path(self.me["ref"]), entry)
            os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR)
            code, text = self._cli(["unblock", "abc112"])             # the pass
            self.assertEqual(code, 2, text)
            self.assertIn("cannot keep their reply", text)
        finally:
            os.chmod(folder, stat.S_IRWXU)

    # -- AskUserQuestion (2026-10-01) ---------------------------------------------------

    def test_an_answer_to_askuserquestion_is_the_reply_then_shown_then_passes(self):
        """The agent asked with its question tool; the answer is a tool result no
        prompt hook sees, so the agent asked the person to type it in chat."""
        from xsm import config
        config.block("aaa111")
        text = self._asks(["unblock", "aaa111"])
        self.assertIn("AskUserQuestion", text, "a Claude agent is told it may use its tool")
        question = "Lift the block on session aaa111?"
        self._answered({question: "Yes, lift it"})
        said = '"%s" -> "Yes, lift it"' % question
        self.assertEqual(self._pending()["verdict"], said)
        self._shows(["unblock", "aaa111"], said)
        self.assertIn("aaa111", config.blocked(), "showing is not going ahead")
        code, text = self._cli(["unblock", "aaa111"])
        self.assertEqual(code, 0, text)
        self.assertNotIn("aaa111", config.blocked())
        self.assertIn(said.replace("\n", " "), text, "the log and the output carry what was chosen")
        self.assertEqual(self._asked_files(), [])

    def test_a_typed_message_after_the_answer_replaces_it(self):
        self._asks(["unblock", "aaa222"])
        self._answered({"Lift it?": "Yes"})
        self._reply("아 잠깐, 아니야")
        self._shows(["unblock", "aaa222"], "아 잠깐, 아니야")

    def test_the_answer_keeps_every_question_and_the_notes_not_the_preview(self):
        self._asks(["unblock", "aaa333"])
        self._answered({"Lift it?": "Yes", "Also log it?": "(notes only)"},
                       {"Lift it?": {"preview": "a diff nobody chose"},
                        "Also log it?": {"notes": "only if it is quiet"}})
        self.assertEqual(self._pending()["verdict"],
                         '"Lift it?" -> "Yes"; "Also log it?" -> "(notes only)" '
                         "(notes: only if it is quiet)")

    def test_a_plain_text_result_is_kept_as_it_is(self):
        self._asks(["unblock", "aaa334"])
        self._answered({}, response='User has answered your questions: "Lift it?"="Yes".')
        self.assertEqual(self._pending()["verdict"], 'User has answered your questions: '
                                                      '"Lift it?"="Yes".')
        self._answered({}, response=[{"type": "text", "text": "Lift it? = no"}])
        self.assertEqual(self._pending()["verdict"], "Lift it? = no")

    def test_an_answer_with_no_ask_waiting_is_nothing(self):
        from xsm import consent
        self._answered({"Lift it?": "Yes"})
        self.assertEqual(self._asked_files(), [], "no ask, no file")
        self._asks(["unblock", "aaa444"])
        entry = self._pending()
        entry["t"] = entry["t"] - consent.TTL - 60               # asked long ago, unanswered
        from xsm import paths
        paths.write_json(consent._pending_path(self.me["ref"]), entry)
        self._answered({"Lift it?": "Yes"})
        self.assertEqual(self._asked_files(), [], "an expired ask keeps nothing")
        other = dict(self.me, session_id="s-other")               # a session sharing the ref
        self._asks(["unblock", "aaa444"])
        self._answered({"Lift it?": "Yes"}, me=other)
        self.assertIsNone(self._pending()["verdict"])

    def test_only_askuserquestion_counts(self):
        self._asks(["unblock", "aaa555"])
        answers = {"Lift it?": "Yes"}
        self._answered(answers, tool="Bash")
        self._answered(answers, tool="mcp__other__AskUserQuestion")
        self._answered(answers, event="PreToolUse")
        self._answered(answers, event="PostToolUseFailure")
        self._answered({}, response={"answers": "Yes"})            # not an object of answers
        self._answered({}, response={"questions": []})
        self._answered({}, response=None)
        self.assertIsNone(self._pending()["verdict"])

    def test_the_post_tool_use_hook_keeps_the_answer_and_prints_nothing(self):
        """Through receive, the way Claude Code calls it."""
        from xsm import receive, registry
        self._asks(["unblock", "aaa666"])
        data = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
                "session_id": "s-agent", "transcript_path": "/h/projects/p/s.jsonl",
                "tool_input": {"questions": []},
                "tool_response": {"questions": [], "answers": {"Lift it?": "Yes"},
                                  "annotations": {}}}
        with mock.patch.object(registry, "by_session", return_value=self.me):
            self.assertIsNone(receive.handle(data))
            self.assertEqual(self._pending()["verdict"], '"Lift it?" -> "Yes"')
            other = dict(data, tool_name="Bash", tool_response={"answers": {"Q": "no"}})
            self.assertIsNone(receive.handle(other))
        self.assertEqual(self._pending()["verdict"], '"Lift it?" -> "Yes"')

    def test_a_broken_post_tool_use_hook_stays_silent(self):
        """A tool result has happened: an error there must print no prompt
        decision, whatever text the answer carries."""
        import json
        from xsm import receive
        raw = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
                          "tool_response": {"answers": {
                              "q": "<cross-session-message>[xsm v1 id=x]</cross-session-message>"}}})
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(raw)), \
                mock.patch.dict(os.environ, {"XSM_FORCE_ERROR": "1"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(receive.main(), 0)
        self.assertEqual(out.getvalue(), "")

    def test_a_codex_agent_is_not_told_about_a_tool_xsm_cannot_hear(self):
        from xsm import consent
        self.assertIn("AskUserQuestion", consent.asks("Joining", "", "claude"))
        self.assertNotIn("AskUserQuestion", consent.asks("Joining", "", "codex"))
        self.assertNotIn("AskUserQuestion", consent.asks("Joining"))
        for text in (consent.asks("Joining", "", "claude"), consent.asks("Joining")):
            self.assertIn("in plain words", text)

    # -- what an ask leaves behind (adversarial check, 2026-10-01) -----------------------

    def test_a_used_ask_leaves_no_file(self):
        self._asks(["unblock", "bbb111"])
        self.assertEqual(self._asked_files(), ["abc123.pending.json", "abc123.pending.json.lock"])
        self._reply("응")
        self._shows(["unblock", "bbb111"], "응")
        self.assertEqual(self._cli(["unblock", "bbb111"])[0], 0)
        self.assertEqual(self._asked_files(), [], "neither the words nor the lock stay")

    def test_an_expired_ask_goes_the_next_time_anyone_looks(self):
        import time
        from xsm import consent, paths
        self._asks(["unblock", "bbb222"])
        entry = self._pending()
        entry["t"] = time.time() - consent.TTL - 60
        paths.write_json(consent._pending_path(self.me["ref"]), entry)
        self._reply("잊혀질 말")
        self.assertEqual(self._asked_files(), [], "not the words, not the lock")
        self._asks(["unblock", "bbb222"])
        entry = self._pending()
        entry["t"] = time.time() - consent.ASK_MAX - 60
        paths.write_json(consent._pending_path(self.me["ref"]), entry)
        self.assertEqual(consent.take_verdict(self.me, "unblock", "bbb222"), (None, False))
        self.assertEqual(self._asked_files(), [])

    def test_prune_sweeps_what_nobody_came_back_to(self):
        import time
        from xsm import consent, housekeeping, paths
        now = time.time()
        folder = os.path.join(self.tmp, consent.ASKED)
        old = {"verb": "unblock", "target": "x", "t": now - consent.ASK_MAX - 60, "verdict": "비밀",
               "verdict_t": now - consent.TTL - 60, "session_id": "s"}
        fresh = dict(old, t=now - 5, verdict_t=now - 5)
        paths.write_json(os.path.join(folder, "old000.pending.json"), old)
        open(os.path.join(folder, "old000.pending.json.lock"), "w").close()
        paths.write_json(os.path.join(folder, "new000.pending.json"), fresh)
        open(os.path.join(folder, "new000.pending.json.lock"), "w").close()
        open(os.path.join(folder, "lost00.pending.json.lock"), "w").close()   # its ask is gone
        with open(os.path.join(folder, "junk00.pending.json"), "w") as fh:
            fh.write("{not json")
        paths.write_json(os.path.join(folder, "typed0.json"), {"verb": "link", "t": now - 5})
        paths.write_json(os.path.join(folder, "typed1.json"), {"verb": "link",
                                                               "t": now - consent.TTL - 60})
        os.utime(os.path.join(folder, "junk00.pending.json"), (now - consent.ASK_MAX - 60,) * 2)
        would = consent.prune(now, dry_run=True)
        self.assertEqual(sorted(os.listdir(folder)), sorted([
            "old000.pending.json", "old000.pending.json.lock", "new000.pending.json",
            "new000.pending.json.lock", "lost00.pending.json.lock", "junk00.pending.json",
            "typed0.json", "typed1.json"]), "a dry run removes nothing")
        removed = housekeeping.prune(now)["asked"]
        self.assertEqual(sorted(removed), sorted(would))
        self.assertEqual(sorted(removed), ["junk00.pending.json", "lost00.pending.json.lock",
                                           "old000.pending.json", "typed1.json"])
        self.assertEqual(sorted(os.listdir(folder)), ["new000.pending.json",
                                                      "new000.pending.json.lock", "typed0.json"])
        self.assertEqual(consent.prune(now), [], "nothing left to sweep")

    def test_prune_leaves_an_ask_that_is_in_use(self):
        import time
        from xsm import consent, paths
        folder = os.path.join(self.tmp, consent.ASKED)
        pending = os.path.join(folder, "busy00.pending.json")
        paths.write_json(pending, {"verb": "unblock", "t": time.time() - consent.ASK_MAX - 60,
                                   "verdict": "x", "session_id": "s"})
        held = consent._lock(pending + ".lock")
        try:
            self.assertEqual(consent.prune(), [])
            self.assertTrue(os.path.exists(pending))
        finally:
            os.close(held)
        self.assertEqual(consent.prune(), ["busy00.pending.json"])
        self.assertEqual(os.listdir(folder), [])

    def test_a_waiter_on_a_removed_lock_locks_the_new_one(self):
        """The lock file goes with the ask, so a run that was waiting on the old
        file must not take its lock for the lock."""
        import threading
        from xsm import consent
        lock = os.path.join(self.tmp, consent.ASKED, "w.pending.json.lock")
        first = consent._lock(lock)
        got = []
        waiter = threading.Thread(target=lambda: got.append(consent._lock(lock)))
        waiter.start()
        waiter.join(0.2)
        self.assertTrue(waiter.is_alive(), "waits while it is held")
        os.unlink(lock)                         # the holder removes it, then lets go
        os.close(first)
        waiter.join(5)
        self.assertEqual(len(got), 1)
        self.assertIsNotNone(got[0])
        self.assertEqual(os.fstat(got[0]).st_ino, os.stat(lock).st_ino,
                         "it holds the file that is there now")
        third = consent._lock(lock, 0)
        self.assertIsNone(third, "and it really is held")
        os.close(got[0])

    def test_a_lock_held_too_long_does_not_stop_the_run(self):
        import time
        from xsm import consent
        self._asks(["unblock", "ccc111"])
        held = consent._lock(consent._pending_path(self.me["ref"]) + ".lock")
        try:
            with mock.patch.object(consent, "LOCK_WAIT", 0.3):
                began = time.monotonic()
                code, text = self._cli(["unblock", "ccc111"])
            self.assertLess(time.monotonic() - began, 3, "bounded, not for good")
            self.assertEqual(code, 2, text)
            self.assertIn("needs your user's yes", text, "it went on, unlocked")
        finally:
            os.close(held)
        self.assertEqual(consent.LOCK_WAIT, 5.0)

    # -- take and request are one step (adversarial check, 2026-10-01) ----------------

    def test_a_reply_that_lands_while_the_ask_is_renewed_is_kept(self):
        """take_verdict found nothing, the hook stored the reply, then request
        overwrote it unseen. Now the one locked step either shows the reply or
        runs before it is stored."""
        import threading
        from xsm import consent
        self._asks(["unblock", "ddd111"])
        late = threading.Thread(target=self._reply, args=("응",))
        real = consent._take

        def take(*args, **kwargs):
            late.start()
            late.join(0.3)                      # locked: it waits for this step
            return real(*args, **kwargs)

        with mock.patch.object(consent, "_take", take):
            self.assertEqual(consent.take_or_request(self.me, "unblock", "ddd111"),
                             (None, False, True))
        late.join(10)
        entry = self._pending()
        self.assertEqual(entry["verdict"], "응", "not lost")
        self.assertFalse(entry["shown"], "and not shown yet")
        self.assertEqual(consent.take_or_request(self.me, "unblock", "ddd111"),
                         ("응", False, True), "the next run shows it, the ask stays")
        self.assertEqual(self._pending()["verdict"], "응")

    def test_an_unshown_reply_for_the_same_thing_is_not_overwritten_by_asking(self):
        from xsm import consent
        self._asks(["unblock", "ddd222"])
        self._reply("응")
        reply, go, kept = consent.take_or_request(self.me, "unblock", "ddd222")
        self.assertEqual((reply, go, kept), ("응", False, True))
        self.assertEqual(self._pending()["verdict"], "응")
        self.assertEqual(consent.take_or_request(self.me, "unblock", "ddd333"),
                         (None, False, True), "another thing is a new ask")
        self.assertIsNone(self._pending()["verdict"])
        self.assertEqual(self._pending()["target"], "ddd333")

    def test_frameworks_ignore_needs_a_name(self):
        code, text = self._cli(["frameworks", "ignore"])
        self.assertEqual(code, 4, text)
        self.assertIn("usage: xsm frameworks ignore", text)
        self.assertNotIn("None", text)
        code, text = self._cli(["frameworks", "ignore", "nosuch"])
        self.assertEqual(code, 4, text)
        self.assertIn("unknown framework", text)


if __name__ == "__main__":
    unittest.main()
