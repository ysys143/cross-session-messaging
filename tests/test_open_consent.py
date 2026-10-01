"""Issue #9, user decision 2026-10-01: a conversation the person wants between their
own sessions is never stopped by asking them for what they already said. The
person's own request counts as the reply; an ask lives half an hour; each thing
asked about keeps its own request; the agent can give the person's words itself
when the hook could not keep them; a hook does not wait long for a lock."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_person_decisions, test_xsm  # noqa: E402

# Module names only: a class imported here by name would run its tests twice.
TempState = test_xsm.TempState
AskHelpers = test_person_decisions.AskHelpers


class _Repos(AskHelpers):
    def setUp(self):
        super().setUp()
        self.a = os.path.realpath(os.path.join(self.tmp, "repo-a"))
        self.b = os.path.realpath(os.path.join(self.tmp, "repo-b"))
        for d in (self.a, self.b):
            os.makedirs(d, exist_ok=True)
        self.me["cwd"] = self.a

    def _links(self):
        from xsm import config
        return config.links()


class RequestAsReplyTest(_Repos, TempState):
    """A4 (i): the person asked for it, so asking again is the ping-pong."""

    def _one_yes(self, prompt):
        """The person says `prompt`; the agent runs `xsm link ../repo-b`, is shown the
        words, reads them, and runs it again: one shown, one passed."""
        self._reply(prompt)
        code, text = self._cli(["link", "../repo-b"])
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "%s"' % prompt, text)
        self.assertEqual(self._links(), [], "shown, not acted on")
        self._waited()
        code, text = self._cli(["link", "../repo-b"])
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self._links()), 1)
        return text

    def test_a_request_that_names_the_folder_and_says_what_to_do_is_the_reply(self):
        for prompt in ("repo-b 세션이랑 연결해서 얘기해봐", "please link repo-b with this one",
                       "connect %s to this session" % self.b, "send repo-b a note about the diff",
                       "repo-b에 이 결과 전달해줘"):
            with self.subTest(prompt):
                self._one_yes(prompt)
                self.assertEqual(self._cli(["unlink", "../repo-b"])[0], 0)

    def test_the_decision_log_says_the_request_was_the_reply(self):
        from xsm import paths
        text = self._one_yes("repo-b 와 연결해줘")
        self.assertIn("repo-b 와 연결해줘", text)
        events = [d["event"] for d in paths.read_jsonl("decisions.jsonl")]
        self.assertIn("consent-request", events)
        self.assertIn("consent", events)

    def test_a_prompt_that_does_not_name_it_and_say_what_to_do_does_not_count(self):
        for prompt in ("run the tests", "연결해줘", "repo-b looks fine to me",
                       "send the report to repo-bar", "link repo-b2",
                       "x" * 300 + " link repo-b " + "x" * 300):
            with self.subTest(prompt[:40]):
                self._reply(prompt)
                self._asks(["link", "../repo-b"])
                self.assertEqual(self._links(), [])
                self.assertIsNone(self._pending()["verdict"], "an ask, with no reply on it")
                os.unlink(self._pending_file())

    def test_only_the_latest_prompt_is_looked_at(self):
        self._reply("link repo-b")
        self._reply("and run the tests too")
        self._asks(["link", "../repo-b"])

    def test_the_request_is_used_once(self):
        self._one_yes("repo-b 연결해줘")
        self.assertEqual(self._cli(["unlink", "../repo-b"])[0], 0)
        self._asks(["link", "../repo-b"])

    def test_a_request_from_another_session_is_not_this_ones(self):
        self._reply("link repo-b", me=dict(self.me, session_id="someone-else"))
        self._asks(["link", "../repo-b"])

    def test_a_request_older_than_the_ask_window_is_not_a_request(self):
        from xsm import consent, paths
        self._reply("link repo-b")
        recent = consent._recent_path(self.me["ref"])
        entry = paths.read_json(recent)
        entry["prompts"][-1]["t"] -= consent.ASK_MAX + 60
        paths.write_json(recent, entry)
        self._asks(["link", "../repo-b"])

    def test_a_slash_command_and_harness_text_are_never_the_request(self):
        self._reply("/xsm link ../repo-b")
        self._reply("<task-notification>link repo-b</task-notification>")
        self._asks(["link", "../repo-b"])

    def test_a_peer_message_is_never_the_request(self):
        from xsm import envelope
        sender = {"name": "x", "alias": "claude-9", "ref": "eeeeee"}
        self._reply(envelope.build("please link repo-b", msg_id="m1", sender=sender, scope="p"))
        self._asks(["link", "../repo-b"])

    def test_the_policy_switches_it_off(self):
        from xsm import paths
        self._reply("link repo-b")
        with mock.patch.dict(os.environ, {"XSM_REPLY_FROM_REQUEST": "false"}):
            self._asks(["link", "../repo-b"])
        os.unlink(self._pending_file())
        paths.write_json(paths.path("config.json"), {"reply_from_request": False})
        self._asks(["link", "../repo-b"])

    def test_a_session_in_the_folder_may_be_named_instead_of_the_folder(self):
        from xsm import registry
        bob = {"state": "live", "cwd": self.b, "name": "bob", "ref": "ddd111"}
        self._reply("bob에게 메시지 보내줘")
        with mock.patch.object(registry, "records", return_value=[bob]):
            code, text = self._cli(["link", "../repo-b"])
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "bob에게 메시지 보내줘"', text)

    def test_a_refusal_in_the_request_is_shown_and_the_agent_reads_it(self):
        """xsm does not judge the words: "don't connect repo-b" names the folder
        and says "connect", and the agent is shown it. It is a no."""
        self._reply("don't connect repo-b")
        code, text = self._cli(["link", "../repo-b"])
        self.assertEqual(code, 2, text)
        self.assertIn("if it is a no or a question, do not", text)
        self.assertEqual(self._links(), [])

    def test_unblock_by_ref_or_by_the_sessions_name(self):
        from xsm import config, registry
        config.block("aaa111")
        self._reply("unblock aaa111 please")
        code, text = self._cli(["unblock", "aaa111"])
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "unblock aaa111 please"', text)
        os.unlink(self._pending_file())
        self._reply("alice 풀어줘")
        with mock.patch.object(registry, "records", return_value=[
                {"state": "live", "cwd": self.b, "name": "alice", "ref": "aaa111"}]):
            code, text = self._cli(["unblock", "aaa111"])
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "alice 풀어줘"', text)

    def test_what_a_hook_keeps_is_the_last_three_prompts_and_not_commands(self):
        from xsm import consent, paths
        for text in ("one", "two", "three", "four", "$xsm list", "/clear"):
            self._reply(text)
        entry = paths.read_json(consent._recent_path(self.me["ref"]))
        self.assertEqual([p["text"] for p in entry["prompts"]], ["two", "three", "four"])
        self.assertEqual(oct(os.stat(consent._recent_path(self.me["ref"])).st_mode & 0o777),
                         "0o600")

    def test_the_same_prompt_seen_by_two_hooks_is_one(self):
        from xsm import consent, paths
        for _ in range(2):
            self._reply("link repo-b")
        entry = paths.read_json(consent._recent_path(self.me["ref"]))
        self.assertEqual(len(entry["prompts"]), 1)

    def test_a_worker_keeps_nothing(self):
        from xsm import consent
        with mock.patch.dict(os.environ, {"XSM_WORKER": "w1"}):
            self._reply("link repo-b")
        self.assertFalse(os.path.exists(consent._recent_path(self.me["ref"])))

    def test_prune_empties_old_prompts_keeps_the_record_that_a_hook_wrote_and_leaves_young_ones(self):
        """2026-10-02: the words go after ASK_MAX, as before. The file stays (empty) for
        HOOK_SEEN_MAX: it is what tells a session with a hook from one without."""
        from xsm import consent, paths
        now = time.time()
        folder = os.path.join(self.tmp, consent.ASKED)
        said = [{"text": "yes", "t": now - consent.ASK_MAX - 60}]
        paths.write_json(os.path.join(folder, "old000.recent.json"),
                         {"session_id": "s", "t": now - consent.ASK_MAX - 60, "prompts": said})
        paths.write_json(os.path.join(folder, "new000.recent.json"),
                         {"session_id": "s", "t": now - 5, "prompts": said})
        paths.write_json(os.path.join(folder, "gone00.recent.json"),
                         {"session_id": "s", "t": now - consent.HOOK_SEEN_MAX - 60, "prompts": []})
        self.assertEqual(consent.prune(now, dry_run=True), ["gone00.recent.json", "old000.recent.json"])
        self.assertEqual(paths.read_json(os.path.join(folder, "old000.recent.json"))["prompts"], said)
        self.assertEqual(consent.prune(now), ["gone00.recent.json", "old000.recent.json"])
        self.assertEqual(sorted(os.listdir(folder)), ["new000.recent.json", "old000.recent.json"])
        old = paths.read_json(os.path.join(folder, "old000.recent.json"))
        self.assertEqual((old["prompts"], old["session_id"]), ([], "s"), "emptied, still there")
        self.assertEqual(paths.read_json(os.path.join(folder, "new000.recent.json"))["prompts"], said)
        self.assertEqual(consent.prune(now), [], "and nothing more to do the next time")


class ReplyFlagTest(_Repos, TempState):
    """A4 (iv): `--reply "<their words>"` when the hook could not keep them."""

    def test_the_flag_is_shown_first_and_then_goes_ahead(self):
        from xsm import config, paths
        config.block("fff111")
        code, text = self._cli(["unblock", "fff111", "--reply", "응 풀어줘"])
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "응 풀어줘"', text, "shown, as a recorded reply is")
        self.assertIn("fff111", config.blocked())
        self._waited()
        code, text = self._cli(["unblock", "fff111", "--reply", "응 풀어줘"])
        self.assertEqual(code, 0, text)
        self.assertIn("approved on your user's reply", text)
        self.assertNotIn("fff111", config.blocked())
        self.assertEqual(self._asked_files(), [], "used up")
        logged = [d for d in paths.read_jsonl("decisions.jsonl") if d.get("event") == "consent-flag"]
        self.assertEqual(len(logged), 1, "the use of the flag is on record, once")

    def _link(self, *extra):
        return self._cli(["link", "../repo-b"] + list(extra))

    def test_an_agent_alone_cannot_approve_when_the_hook_keeps_prompts(self):
        """2026-10-02: two runs of `--reply "yes please"` linked the folders and logged
        the person as the one who said it."""
        from xsm import paths
        self._reply("run the tests please")             # the hook works: it keeps their words
        for _ in range(3):
            code, text = self._link("--reply", "yes please")
            self.assertEqual(code, 2, text)
            self.assertIn("--reply is ignored", text)
            self.assertIn("your user's own words", text)
            self.assertIn("needs your user's yes", text, "and it is asked as it would be")
            self.assertNotIn('your user replied: "yes please"', text)
        self.assertEqual(self._links(), [])
        self.assertIsNone(self._pending()["verdict"])
        events = [d.get("event") for d in paths.read_jsonl("decisions.jsonl")]
        self.assertNotIn("consent-flag", events)
        self.assertNotIn("consent", events)

    def test_the_words_a_person_wrote_pass_and_what_is_kept_is_their_wording(self):
        from xsm import config, paths
        self._reply("yes, go ahead and connect them")   # before the ask: no pending to attach to
        code, text = self._link("--reply", "YES,   go ahead and connect them, thanks to the agent")
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "yes, go ahead and connect them"', text,
                      "the person's words, not the agent's wording")
        self.assertNotIn("thanks to the agent", text)
        self.assertEqual(self._links(), [])
        self._waited()
        code, text = self._link("--reply", "go ahead and connect")
        self.assertEqual(code, 0, text)
        self.assertIn("approved on your user's reply", text)
        self.assertIn("given with --reply by the agent", text)
        (link,) = config.links()
        self.assertIn("agent-supplied", link["by"], "the record says it was not typed into xsm")
        self.assertIn("yes, go ahead and connect them", link["by"])
        consents = [d for d in paths.read_jsonl("decisions.jsonl") if d.get("event") == "consent"]
        self.assertEqual(consents[-1]["via"], "agent-supplied")
        flags = [d for d in paths.read_jsonl("decisions.jsonl") if d.get("event") == "consent-flag"]
        self.assertEqual(flags[-1]["via"], "agent-supplied")

    def test_a_session_the_hook_never_wrote_for_is_heard_through_the_flag(self):
        from xsm import config, consent, paths
        self.assertFalse(os.path.exists(consent._recent_path(self.me["ref"])))
        self._link("--reply", "응 연결해")
        self._waited()
        code, text = self._link("--reply", "응 연결해")
        self.assertEqual(code, 0, text)
        (link,) = config.links()
        self.assertIn("agent-supplied", link["by"])
        self.assertIn("응 연결해", link["by"])
        self.assertEqual([d for d in paths.read_jsonl("decisions.jsonl")
                          if d.get("event") == "consent"][-1]["via"], "agent-supplied")

    def test_a_hook_that_wrote_once_is_not_forgotten_when_its_words_age_out(self):
        """The person went quiet for half an hour and the hook's words were pruned: an
        agent still cannot say it never had a hook."""
        from xsm import consent, paths
        self._reply("run the tests")
        old = time.time() - consent.ASK_MAX - 60
        recent = consent._recent_path(self.me["ref"])
        paths.write_json(recent, {"session_id": self.me["session_id"], "t": old,
                                  "prompts": [{"text": "run the tests", "t": old}]})
        consent.prune()
        self.assertEqual(paths.read_json(recent)["prompts"], [])
        code, text = self._link("--reply", "yes")
        self.assertIn("--reply is ignored", text)
        self.assertEqual(self._links(), [])

    def test_a_prompt_already_taken_for_a_request_or_too_old_is_not_theirs_to_repeat(self):
        from xsm import consent, paths
        self._reply("yes do it")
        recent = consent._recent_path(self.me["ref"])
        entry = paths.read_json(recent)
        entry["prompts"][-1]["used"] = True
        paths.write_json(recent, entry)
        self.assertIn("--reply is ignored", self._link("--reply", "yes do it")[1])
        entry["prompts"][-1].pop("used")
        entry["prompts"][-1]["t"] -= consent.ASK_MAX + 60
        paths.write_json(recent, entry)
        self.assertIn("--reply is ignored", self._link("--reply", "yes do it")[1])

    def test_the_verdict_the_hook_stored_on_the_ask_counts_and_the_agents_own_does_not(self):
        """What the hook keeps from an AskUserQuestion answer is no prompt, but it is theirs."""
        from xsm import config, consent, paths
        self._asks(["unblock", "ccc111"])
        consent.note_verdict(self.me, "Yes -> unblock it")      # the hook, from the form's answer
        config.block("ccc111")
        code, text = self._cli(["unblock", "ccc111", "--reply", "yes -> UNBLOCK it"])
        self.assertNotIn("--reply is ignored", text)
        self.assertIn('your user replied: "Yes -> unblock it"', text)
        self.assertNotIn("via", self._pending(), "it is still the hook's verdict")
        os.unlink(self._pending_file())
        # An agent's own flag is not a hook's verdict, and does not vouch for itself.
        self._reply("unrelated chatter")
        self._cli(["unblock", "ccc111", "--reply", "yes"])
        self.assertEqual(self._pending()["verdict"], None)

    def test_the_same_words_given_again_do_not_reset_the_show(self):
        from xsm import config
        config.block("fff222")
        self._cli(["unblock", "fff222", "--reply", "응"])
        code, text = self._cli(["unblock", "fff222", "--reply", "응"])
        self.assertEqual(code, 2, "within the delay: still the show, not a pass")
        self.assertIn('your user replied: "응"', text)

    def test_other_words_replace_the_first_and_are_shown_too(self):
        from xsm import config
        config.block("fff333")
        self._cli(["unblock", "fff333", "--reply", "글쎄"])
        self._waited()
        code, text = self._cli(["unblock", "fff333", "--reply", "아니 풀어줘"])
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "아니 풀어줘"', text)

    def test_the_flag_runs_the_same_show_step_on_every_asking_command(self):
        from xsm import cli
        sub = cli.build_parser()._subparsers._group_actions[0].choices
        for name in cli.ASKING:
            with self.subTest(name):
                self.assertIn("--reply", sub[name]._option_string_actions)

    def test_the_policy_switches_the_flag_off(self):
        from xsm import config, paths
        for how in ("environment", "config"):
            with self.subTest(how):
                if how == "config":
                    paths.write_json(paths.path("config.json"), {"reply_flag": False})
                config.block("fff444")
                env = {"XSM_REPLY_FLAG": "0"} if how == "environment" else {}
                with mock.patch.dict(os.environ, env):
                    code, text = self._cli(["unblock", "fff444", "--reply", "응"])
                self.assertEqual(code, 2, text)
                self.assertIn("--reply is switched off", text)
                self.assertNotIn('your user replied', text)
                self.assertIn("needs your user's yes", text, "asked as without it")
                self.assertIn("fff444", config.blocked())
                for p in list(os.listdir(os.path.join(self.tmp, "asked"))):
                    os.unlink(os.path.join(self.tmp, "asked", p))

    def test_it_does_not_reach_a_worker_or_a_session_with_no_ref(self):
        from xsm import consent
        with mock.patch.dict(os.environ, {"XSM_WORKER": "w1"}):
            consent.supplied = "응"
            self.assertEqual(consent.take_or_request(self.me, "unblock", "abc123"),
                             (None, False, False))
        consent.supplied = "응"
        self.assertEqual(consent.take_or_request(dict(self.me, ref=None), "unblock", "abc123"),
                         (None, False, False))


class OneSlotPerThingTest(_Repos, TempState):
    """A4 (iii)."""

    def test_each_thing_asked_about_has_its_own_file(self):
        from xsm import consent
        self._asks(["unblock", "aaa111"])
        self._asks(["unblock", "aaa222"])
        files = consent.pending_files(self.me["ref"])
        self.assertEqual(len(files), 2)
        self.assertEqual(len({os.path.basename(f) for f in files}), 2)

    def test_the_reply_is_written_to_every_open_ask_and_each_shows_it(self):
        self._asks(["unblock", "aaa111"])
        self._asks(["join", "demo"])
        self._reply("응")
        code, text = self._cli(["unblock", "aaa111"])
        self.assertIn('your user replied: "응"', text)
        code, text = self._cli(["join", "demo"])
        self.assertIn('your user replied: "응"', text)

    def test_an_expired_ask_does_not_keep_the_reply_of_another(self):
        from xsm import consent, paths
        self._asks(["unblock", "aaa111"])
        self._asks(["unblock", "aaa222"])
        old = consent._pending_path(self.me["ref"], consent.digest("unblock", "aaa111"))
        entry = paths.read_json(old)
        entry["t"] -= consent.ASK_MAX + 60
        paths.write_json(old, entry)
        self._reply("응")
        self.assertFalse(os.path.exists(old), "gone, with its lock")
        self.assertEqual(len(consent.pending_files(self.me["ref"])), 1)

    def test_a_single_slot_an_older_xsm_wrote_is_still_read_and_still_written(self):
        """Version mix: a session whose ask was made by the previous release."""
        from xsm import consent, paths
        legacy = consent._pending_path(self.me["ref"])
        paths.write_json(legacy, {"verb": "unblock", "target": "old111", "here": None,
                                  "cwd": self.tmp, "t": time.time() - 20,
                                  "session_id": "s-agent", "verdict": None})
        self._reply("응")
        self.assertEqual(paths.read_json(legacy)["verdict"], "응", "the hook writes to it too")
        code, text = self._cli(["unblock", "old111"])
        self.assertEqual(code, 2, text)
        self.assertIn('your user replied: "응"', text)
        entry = paths.read_json(legacy)
        entry["shown_t"] -= consent.SHOW_DELAY + 1
        paths.write_json(legacy, entry)
        self.assertEqual(self._cli(["unblock", "old111"])[0], 0)
        self.assertFalse(os.path.exists(legacy), "used up")


class HookLockWaitTest(_Repos, TempState):
    """B7: a hook runs on every prompt of every session; a command may wait longer."""

    def test_the_hook_waits_one_second_and_a_command_five(self):
        from xsm import consent
        self.assertEqual(consent.HOOK_LOCK_WAIT, 1.0)
        self.assertEqual(consent.LOCK_WAIT, 5.0)
        self._asks(["unblock", "bbb111"])
        waits = []
        with mock.patch.object(consent, "_lock", side_effect=lambda lock, wait=None:
                               waits.append(wait)):
            consent.note_verdict(self.me, "응")
            consent.take_or_request(self.me, "unblock", "bbb111")
        self.assertEqual(waits, [consent.HOOK_LOCK_WAIT, None],
                         "the hook's own wait; the command's is the default")

    def test_a_held_lock_costs_the_hook_about_a_second_and_loses_nothing_else(self):
        from xsm import consent
        self._asks(["unblock", "bbb222"])
        held = consent._lock(self._pending_file() + ".lock")
        try:
            with mock.patch.object(consent, "HOOK_LOCK_WAIT", 0.3):
                began = time.monotonic()
                self.assertTrue(consent.note_verdict(self.me, "응"))
            self.assertLess(time.monotonic() - began, 3, "bounded by the hook's wait")
        finally:
            os.close(held)


if __name__ == "__main__":
    unittest.main()
