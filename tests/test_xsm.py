"""Unit tests for the parts that decide something: wire format, addressing,
scope, the hook's fallback, and the settings merge.

Run: python3 -m unittest discover -s tests -v
Everything here works on a temporary XSM_HOME; no session is contacted.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _run_cli(argv: list) -> str:
    from xsm import cli
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.main(argv)
    return out.getvalue()


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xsm-test-")
        os.environ["XSM_HOME"] = self.tmp
        for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
            del sys.modules[mod]
        from xsm import paths, registry
        paths.HOME = self.tmp
        paths.ensure_home()
        # Never scan this machine's real processes from a test: it made runs
        # depend on whatever Codex happened to be open (and lsof was slow).
        registry._running_codex = lambda: []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class EnvelopeTest(TempState):
    def test_round_trip_keeps_header_and_body(self):
        from xsm import envelope
        sender = {"name": "a", "alias": "claude-4", "ref": "aaaaaa", "socket": "/tmp/s.sock",
                  "session_id": "s1", "permission_mode": "bypassPermissions"}
        wire = envelope.build("hello", msg_id="m1", sender=sender, scope="repo:x", kind="task")
        parsed = envelope.parse(wire)
        self.assertTrue(parsed.peer)
        self.assertEqual(parsed.body, "hello")
        self.assertEqual(parsed.header["id"], "m1")
        self.assertEqual(parsed.header["kind"], "task")
        self.assertEqual(parsed.attrs["from-mode"], "bypass")

    def test_from_mode_follows_the_registry_not_the_caller(self):
        from xsm import envelope
        prompting = {"name": "a", "alias": "h", "ref": "r", "permission_mode": "auto"}
        unknown = {"name": "a", "alias": "h", "ref": "r"}
        self.assertIn('from-mode="prompting"',
                      envelope.build("x", msg_id="i", sender=prompting, scope="s"))
        self.assertNotIn("from-mode", envelope.build("x", msg_id="i", sender=unknown, scope="s"))

    def test_plain_text_is_not_a_peer_message(self):
        from xsm import envelope
        parsed = envelope.parse("just typing")
        self.assertFalse(parsed.peer)
        self.assertEqual(parsed.header, {})

    def test_envelope_without_header_is_still_peer(self):
        """The case S8-g2 measured: an envelope we did not write."""
        from xsm import envelope
        parsed = envelope.parse('<cross-session-message from-mode="bypass">\nhi\n'
                                '</cross-session-message>')
        self.assertTrue(parsed.peer)
        self.assertEqual(parsed.header, {})

    def test_traceparent_rides_along_when_there_is_one(self):
        from xsm import envelope
        sender = {"name": "a", "alias": "claude-4", "ref": "aaaaaa"}
        tp = "00-%s-%s-01" % ("a" * 32, "b" * 16)
        wire = envelope.build("hi", msg_id="m1", sender=sender, scope="s", traceparent=tp)
        self.assertEqual(envelope.parse(wire).header["traceparent"], tp)
        self.assertEqual(envelope.parse(wire).body, "hi")
        plain = envelope.build("hi", msg_id="m1", sender=sender, scope="s")
        self.assertNotIn("traceparent", plain)
        self.assertNotIn("traceparent", envelope.parse(plain).header)


class ScopeTest(TempState):
    def test_same_directory_is_in_scope_and_different_ones_are_not(self):
        from xsm import config
        here = {"cwd": self.tmp, "runtime": "claude", "alias": "claude-3"}
        there = {"cwd": os.path.dirname(self.tmp), "runtime": "claude", "alias": "claude-4"}
        self.assertIsNotNone(config.scope_for(here, dict(here))[0])
        self.assertIsNone(config.scope_for(here, there)[0])

    def test_explicit_scope_crosses_directories(self):
        from xsm import config, paths
        a = {"cwd": os.path.join(self.tmp, "a"), "runtime": "claude", "alias": "claude-3"}
        b = {"cwd": os.path.join(self.tmp, "b"), "runtime": "codex", "alias": "codex"}
        paths.write_json(paths.path(config.CONFIG), {"scopes": [
            {"id": "pair", "members": [{"runtime": "claude", "cwd": os.path.join(self.tmp, "a")},
                                       {"runtime": "codex", "cwd": os.path.join(self.tmp, "b")}]}]})
        self.assertEqual(config.scope_for(a, b)[0], "pair")


class ResolveTest(TempState):
    def _register(self, name, alias, session_id, state="live"):
        from xsm import paths, registry
        home = os.path.join(self.tmp, "homes", alias)
        os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
        rec = registry.upsert("claude", home, session_id, os.getpid(), self.tmp, name=name)
        paths.write_json(os.path.join(home, "sessions", "%d.json" % os.getpid()),
                         {"name": name, "messagingSocketPath": "", "sessionId": session_id})
        return rec

    def test_ambiguous_names_refuse_with_candidates(self):
        from xsm import resolve
        self._register("twin", "claude-3", "s1")
        self._register("twin", "claude-4", "s2")
        found = resolve.resolve("twin", include_offline=True)
        self.assertEqual(found.status, "ambiguous")
        self.assertEqual(len(found.candidates), 2)

    def test_home_qualifier_picks_one(self):
        from xsm import resolve
        self._register("twin", "claude-3", "s1")
        self._register("twin", "claude-4", "s2")
        found = resolve.resolve("twin@claude-4", include_offline=True)
        self.assertEqual(found.status, "resolved")
        self.assertEqual(found.record["alias"], "claude-4")

    def test_at_sign_inside_a_name_is_not_a_qualifier(self):
        """Codex allows @ in thread names (S7), so only a known home qualifies."""
        from xsm import resolve
        self._register("team@standup", "claude-3", "s3")
        found = resolve.resolve("team@standup", include_offline=True)
        self.assertEqual(found.status, "resolved")


class HookFallbackTest(TempState):
    """A broken hook must refuse peer messages and never block the user."""

    def _run(self, prompt):
        env = dict(os.environ, XSM_FORCE_ERROR="1", XSM_HOME=self.tmp)
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "x",
                              "cwd": self.tmp, "prompt": prompt})
        return subprocess.run([sys.executable, os.path.join(REPO, "hooks", "xsm-hook.py")],
                              input=payload, capture_output=True, text=True, env=env)

    def test_peer_message_is_blocked(self):
        out = self._run('<cross-session-message from-mode="bypass">\n[xsm v1 id=1]\nhi\n'
                        '</cross-session-message>')
        self.assertIn('"decision": "block"', out.stdout)
        self.assertEqual(out.returncode, 0)

    def test_human_prompt_passes(self):
        out = self._run("please refactor this file")
        self.assertEqual(out.stdout.strip(), "")
        self.assertEqual(out.returncode, 0)


class InstallTest(TempState):
    def test_merge_keeps_foreign_hooks_and_uninstall_restores(self):
        from xsm import install, paths
        home = os.path.join(self.tmp, "claude-9")
        os.makedirs(home)
        original = {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "sh ~/other-hook.sh"}]}]},
            "crossSessionInbound": "accept"}
        target = os.path.join(home, "settings.json")
        paths.write_json(target, original, mode=0o644)

        install.apply(home, "claude")
        after = paths.read_json(target)
        commands = [h["command"] for g in after["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertIn("sh ~/other-hook.sh", commands)
        self.assertTrue(any(install.MARKER in c for c in commands))
        self.assertEqual(after["crossSessionInbound"], "accept")

        install.remove(home, "claude")
        self.assertEqual(paths.read_json(target), original)



class ForecastTest(TempState):
    """The native gate Claude applies before our hook (S1): an explicit
    setting wins, then permission-mode parity."""

    def _home_with(self, value):
        from xsm import paths
        home = os.path.join(self.tmp, "home-%s" % (value or "none"))
        os.makedirs(home, exist_ok=True)
        if value:
            paths.write_json(os.path.join(home, "settings.json"), {"crossSessionInbound": value})
        return home

    def test_accept_setting_beats_mode_mismatch(self):
        from xsm import send
        a = {"runtime": "claude", "permission_mode": "bypassPermissions"}
        b = {"runtime": "claude", "permission_mode": "auto", "home": self._home_with("accept")}
        self.assertEqual(send.native_forecast(a, b)[0], "accept")

    def test_mode_mismatch_without_a_setting_is_held(self):
        from xsm import send
        a = {"runtime": "claude", "permission_mode": "bypassPermissions"}
        b = {"runtime": "claude", "permission_mode": "auto", "home": self._home_with(None)}
        verdict, why = send.native_forecast(a, b)
        self.assertEqual(verdict, "hold")
        self.assertIn("crossSessionInbound", why)

    def test_matching_modes_pass(self):
        from xsm import send
        a = {"runtime": "claude", "permission_mode": "auto"}
        b = {"runtime": "claude", "permission_mode": "auto", "home": self._home_with(None)}
        self.assertEqual(send.native_forecast(a, b)[0], "accept")

    def test_codex_target_has_no_such_gate(self):
        from xsm import send
        self.assertEqual(send.native_forecast({"runtime": "claude"}, {"runtime": "codex"})[0], "n/a")



class UnknownSelfTest(TempState):
    """A hook that cannot tell which session it guards must refuse peer
    messages: scope is unchecked, so passing would make that session an open
    door. Driven through handle() directly because whether the process tree
    happens to contain a real `claude` ancestor is environment-dependent.
    """

    def _message(self):
        from xsm import envelope
        sender = {"name": "send", "alias": "claude-3", "ref": "aaaaaa",
                  "session_id": "s1", "permission_mode": "auto"}
        return envelope.build("hi", msg_id="m1", sender=sender, scope="dir:x")

    def _handle(self, prompt):
        from xsm import receive
        receive.register = lambda data, runtime: None        # identity unknown
        return receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "r1",
                               "cwd": self.tmp, "prompt": prompt, "session_title": "recv"})

    def test_peer_message_is_refused(self):
        out = self._handle(self._message())
        self.assertEqual(out["decision"], "block")
        self.assertIn("cannot identify this session", out["reason"])
        self.assertTrue(os.listdir(os.path.join(self.tmp, "held")), "body must be kept")

    def test_human_prompt_is_untouched(self):
        self.assertIsNone(self._handle("my own prompt"))


class InterpreterPinTest(TempState):
    """Hooks run under a pinned absolute interpreter, never a launcher."""

    def test_absolute_path_is_taken_as_is(self):
        from xsm import install
        self.assertEqual(install.resolve_python(sys.executable), os.path.realpath(sys.executable))

    def test_unknown_spec_is_refused(self):
        from xsm import install
        with self.assertRaises(ValueError):
            install.resolve_python("definitely-not-a-python-9.9")

    def test_pin_is_recorded_and_used_in_the_hook_command(self):
        from xsm import install
        install.pin_python(sys.executable)
        self.assertEqual(install.pinned_python(), sys.executable)
        self.assertIn(sys.executable, install.hook_command("claude", "SessionStart"))
        self.assertTrue(install.hook_command("claude", "SessionStart").endswith(install.MARKER))

class IdempotenceTest(TempState):
    """Installing twice changes nothing and leaves no second backup, a dry run
    writes nothing at all, and an interpreter pin survives a later install that
    does not mention one."""

    def _home(self):
        from xsm import paths
        home = os.path.join(self.tmp, "claude-idem")
        os.makedirs(home, exist_ok=True)
        paths.write_json(os.path.join(home, "settings.json"), {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "sh ~/other.sh"}]}]}}, mode=0o644)
        return home

    def _backups(self, home):
        return [f for f in os.listdir(home) if ".xsm-backup-" in f]

    def test_second_install_is_a_no_op(self):
        from xsm import install, paths
        home = self._home()
        install.apply(home, "claude")
        first = paths.read_json(os.path.join(home, "settings.json"))
        second = install.apply(home, "claude")
        self.assertTrue(second.get("unchanged"))
        self.assertEqual(paths.read_json(os.path.join(home, "settings.json")), first)
        self.assertEqual(len(self._backups(home)), 1)

    def test_a_duplicated_or_stale_marked_group_collapses_to_one(self):
        from xsm import install, paths
        home = self._home()
        install.apply(home, "claude")
        target = os.path.join(home, "settings.json")
        data = paths.read_json(target)
        data["hooks"]["SessionStart"].append(
            {"hooks": [{"type": "command", "command": "/old/python /old/xsm-hook.py %s" % install.MARKER}]})
        paths.write_json(target, data, mode=0o644)
        install.apply(home, "claude")
        groups = paths.read_json(target)["hooks"]["SessionStart"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["hooks"][0]["command"], install.hook_command("claude", "SessionStart"))

    def test_round_trip_leaves_the_file_byte_identical(self):
        """A settings file people edit by hand must not be reformatted."""
        from xsm import install
        home = os.path.join(self.tmp, "claude-fmt")
        os.makedirs(home, exist_ok=True)
        target = os.path.join(home, "settings.json")
        original = ('{\n  "crossSessionInbound": "accept",\n  "hooks": {\n    "UserPromptSubmit": [\n'
                    '      {\n        "hooks": [\n          {\n            "type": "command",\n'
                    '            "command": "sh ~/other.sh"\n          }\n        ]\n      }\n'
                    '    ]\n  }\n}\n')
        open(target, "w").write(original)
        install.apply(home, "claude")
        install.remove(home, "claude")
        self.assertEqual(open(target).read(), original)

    def test_pin_survives_an_install_without_python(self):
        from xsm import install
        install.pin_python("/usr/bin/python3")
        self.assertEqual(install.resolve_python(None), "/usr/bin/python3")


class LauncherTest(TempState):
    """bin/xsm is meant to be symlinked onto PATH, so it must follow the link
    chain to find the package — resolving $0's directory alone once made
    `~/.local/bin/xsm` look for the code in ~/.local."""

    def _run(self, path, *args):
        env = dict(os.environ, XSM_HOME=self.tmp)
        return subprocess.run([path, *args], capture_output=True, text=True, env=env, cwd="/")

    def test_direct_call_works(self):
        out = self._run(os.path.join(REPO, "bin", "xsm"), "list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_symlinked_call_works(self):
        link = os.path.join(self.tmp, "xsm")
        os.symlink(os.path.join(REPO, "bin", "xsm"), link)
        out = self._run(link, "list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_symlink_to_a_symlink_works(self):
        first = os.path.join(self.tmp, "xsm-one")
        second = os.path.join(self.tmp, "xsm-two")
        os.symlink(os.path.join(REPO, "bin", "xsm"), first)
        os.symlink(first, second)
        out = self._run(second, "list")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_copy_away_from_the_package_says_so(self):
        lonely = os.path.join(self.tmp, "lonely", "bin")
        os.makedirs(lonely)
        copied = os.path.join(lonely, "xsm")
        shutil.copy2(os.path.join(REPO, "bin", "xsm"), copied)
        out = self._run(copied, "list")
        self.assertEqual(out.returncode, 4)
        self.assertIn("no package at", out.stderr)


class ListFromAPlainTerminalTest(TempState):
    """`xsm list` run outside any session has no "us" to compare against, so it
    must not label every row out-of-scope."""

    def test_no_scope_verdict_without_a_session(self):
        from xsm import cli, paths, registry
        home = os.path.join(self.tmp, "homes", "claude-4")
        os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
        registry.upsert("claude", home, "s1", os.getpid(), self.tmp, name="solo")
        paths.write_json(os.path.join(home, "sessions", "%d.json" % os.getpid()),
                         {"name": "solo", "sessionId": "s1", "messagingSocketPath": ""})
        for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET"):
            os.environ.pop(var, None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--all"])
        printed = out.getvalue()
        self.assertIn("not a registered session", printed)
        self.assertNotIn("out-of-scope", printed)


class SuggestionTest(TempState):
    """A name that matches nothing comes back with what does exist, so a caller
    working from a stale list does not have to fetch one."""

    def test_unknown_name_lists_registered_sessions(self):
        from xsm import paths, registry, resolve
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="worker")
        found = resolve.resolve("nobody-here")
        self.assertEqual(found.status, "not-found")
        self.assertEqual([c["name"] for c in found.candidates], ["worker"])
        self.assertIn("worker@codex", resolve.describe(found.candidates))

    def test_unknown_ref_also_suggests(self):
        from xsm import registry, resolve
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="worker")
        self.assertEqual(resolve.resolve("ref:zzzzzz").candidates[0]["name"], "worker")


class HeldRecordTest(TempState):
    """A refused message must say where it came from, even when it carried no
    xsm header — an injection's only trace is the envelope's reply address."""

    def test_injection_records_its_reply_address(self):
        from xsm import envelope, paths, receive
        receive.register = lambda data, runtime: None
        prompt = ('<%s from="uds:/tmp/cc-socks/999.sock" from-mode="prompting">\nraw\n</%s>'
                  % (envelope.TAG, envelope.TAG))
        receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "r1",
                        "cwd": self.tmp, "prompt": prompt, "session_title": "recv"})
        held = os.listdir(paths.path(paths.HELD))
        self.assertEqual(len(held), 1)
        record = paths.read_json(paths.path(paths.HELD, held[0]))
        self.assertEqual(record["from"], "uds:/tmp/cc-socks/999.sock")
        self.assertEqual(record["body"], "raw")


class CodexReceiverNameTest(TempState):
    """A Codex thread's name lives in its state DB, not in the hook input, so
    the hook must read it back before recording who received a message.
    Observed: decisions for a named Codex receiver said `receiver: null`."""

    def test_held_record_names_the_codex_receiver(self):
        import sqlite3
        from xsm import envelope, paths, receive, registry
        home = os.path.join(self.tmp, "codex-named")
        os.makedirs(home)
        con = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
        con.execute("create table threads (id text, name text)")
        con.execute("insert into threads values ('t1', 'reviewer')")
        con.commit()
        con.close()
        receive.register = lambda data, runtime: registry.upsert(
            "codex", home, "t1", os.getpid(), self.tmp)
        prompt = "<%s>\nraw\n</%s>" % (envelope.TAG, envelope.TAG)
        receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "t1",
                        "cwd": self.tmp, "prompt": prompt,
                        "transcript_path": home + "/sessions/2026/t1.jsonl"})
        held = os.listdir(paths.path(paths.HELD))
        record = paths.read_json(paths.path(paths.HELD, held[0]))
        self.assertEqual(record["receiver"], "reviewer")


class SessionFolderTest(TempState):
    def test_a_claude_session_stays_in_the_folder_it_started_in(self):
        """A `cd` in its Bash tool changed the hook's cwd, and `xsm who` put
        the session in that subfolder (2026-09-23)."""
        from unittest import mock
        from xsm import receive
        data = {"cwd": os.path.join(self.tmp, "sub")}
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.tmp}):
            self.assertEqual(receive.session_folder("claude", data), self.tmp)
            self.assertEqual(receive.session_folder("codex", data), data["cwd"],
                             "Codex's cwd already is its start folder")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            self.assertEqual(receive.session_folder("claude", data), data["cwd"])


class CodexInboxTest(TempState):
    """A Codex session takes its queue only between turns. In S10 collab run 4
    the Codex worker never ended one (it polled with sleep) and read none of
    the six messages sent to it. `xsm inbox` hands them over mid-turn."""

    def setUp(self):
        super().setUp()
        from xsm import adapters, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        self.a = registry.upsert("codex", home, "t-a", os.getpid(), self.tmp, name="a")
        self.b = registry.upsert("codex", home, "t-b", os.getpid(), self.tmp, name="b")
        self.queued = []
        adapters.to_codex = lambda home, thread, content: self.queued.append(content) or "ok"

    def _send(self, text="hello"):
        from xsm import send
        return send.send("b", text, sender=self.a)

    def test_a_message_waits_and_is_read_once(self):
        from xsm import inbox, ledger, receive
        r = self._send("phase 2 started")
        self.assertEqual(r.status, "sent-unconfirmed")
        self.assertEqual(inbox.count("t-b"), 1)
        texts = receive.take_inbox(self.b)
        self.assertEqual(len(texts), 1)
        self.assertIn("phase 2 started", texts[0])
        self.assertIn("another agent session", texts[0], "the same framing the hook adds")
        self.assertEqual(ledger.status(r.msg_id or "")["status"], "delivered")
        self.assertEqual(receive.take_inbox(self.b), [], "taken once")

    def test_the_queue_copy_arriving_later_is_dropped_not_held(self):
        from xsm import paths, receive
        self._send()
        receive.take_inbox(self.b)
        receive.register = lambda data, runtime: self.b
        out = receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "t-b",
                              "turn_id": "x", "cwd": self.tmp, "prompt": self.queued[0]})
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["decision"], "block", "refusing is what drops it from Codex")
        self.assertIn("already received", out["reason"])
        self.assertEqual(os.listdir(paths.path(paths.HELD)), [], "the session has it; not held")

    def test_a_message_the_hook_delivered_is_gone_from_the_inbox(self):
        from xsm import inbox, receive
        self._send()
        receive.register = lambda data, runtime: self.b
        out = receive.handle({"hook_event_name": "UserPromptSubmit", "session_id": "t-b",
                              "turn_id": "x", "cwd": self.tmp, "prompt": self.queued[0]})
        self.assertNotEqual((out or {}).get("decision"), "block")
        self.assertEqual(inbox.count("t-b"), 0)
        self.assertEqual(receive.take_inbox(self.b), [])

    def test_the_inbox_runs_the_same_checks_as_the_hook(self):
        from xsm import config, ledger, receive
        r = self._send()
        config.blocked = lambda: {self.a["ref"]}
        texts = receive.take_inbox(self.b)
        self.assertEqual(len(texts), 1)
        self.assertIn("refused", texts[0])
        self.assertNotIn("hello", texts[0])
        self.assertEqual(ledger.status(r.msg_id or "")["status"], "held")

    def test_a_failed_send_leaves_no_copy(self):
        from xsm import adapters, inbox

        def refuse(home, thread, content):
            raise adapters.DeliveryError("sandbox-blocked", "Operation not permitted")
        adapters.to_codex = refuse
        self.assertEqual(self._send().status, "error")
        self.assertEqual(inbox.count("t-b"), 0)

    def test_every_xsm_command_tells_a_codex_session_what_is_waiting(self):
        from xsm import cli
        self._send()
        self.addCleanup(lambda v=os.environ.get("CODEX_THREAD_ID"):
                        os.environ.__setitem__("CODEX_THREAD_ID", v) if v is not None
                        else os.environ.pop("CODEX_THREAD_ID", None))
        os.environ["CODEX_THREAD_ID"] = "t-b"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["ledger", "--compact"])
        self.assertIn("1 message from other sessions waiting", err.getvalue())
        self.assertIn("xsm inbox", err.getvalue())


class ProjectJoinTest(TempState):
    """Sessions in different projects talk once both projects have joined the
    same xsm project by name. One side joining is not enough: that would let a
    project pull another in without its consent."""

    def _dirs(self):
        a, b = os.path.join(self.tmp, "proj-a"), os.path.join(self.tmp, "proj-b")
        os.makedirs(os.path.join(a, "sub"))
        os.makedirs(b)
        return a, b

    def test_both_sides_must_join(self):
        from xsm import config
        a, b = self._dirs()
        sa, sb = {"cwd": os.path.join(a, "sub")}, {"cwd": b}
        self.assertIsNone(config.scope_for(sa, sb)[0])
        config.join("demo", a)
        scope, reason = config.scope_for(sa, sb)
        self.assertIsNone(scope, "one side alone must not open it")
        self.assertIn("%s has not joined project demo" % os.path.realpath(b), reason)
        config.join("demo", b)
        self.assertEqual(config.scope_for(sa, sb)[0], "demo")

    def test_named_project_is_joined_in_addition_to_the_default(self):
        from xsm import config
        import subprocess
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "sub"))
        subprocess.run(["git", "init", "-q", repo], check=True)
        _, other = self._dirs()
        config.join("demo", repo)
        config.join("demo", other)
        inside = config.scope_for({"cwd": repo}, {"cwd": os.path.join(repo, "sub")})
        self.assertEqual(inside[0], "repo:repo", "joining must not change the default scope")
        self.assertEqual(config.scope_for({"cwd": repo}, {"cwd": other})[0], "demo")
        self.assertEqual(config.default_project(other), ("dir:proj-b", os.path.realpath(other)))

    def test_join_is_idempotent_and_leave_closes_it(self):
        from xsm import config
        a, b = self._dirs()
        self.assertTrue(config.join("demo", a)[1])
        self.assertFalse(config.join("demo", a)[1])
        config.join("demo", b)
        self.assertTrue(config.leave("demo", b))
        self.assertIsNone(config.scope_for({"cwd": a}, {"cwd": b})[0])
        self.assertTrue(config.leave("demo", a))
        self.assertEqual(config.projects(), [])

    def test_root_does_not_match_a_sibling_prefix(self):
        from xsm import config
        a, _ = self._dirs()
        self.assertFalse(config.member_matches({"root": a}, {"cwd": a + "-other"}))

    def test_bad_name_and_hand_written_scope_are_refused(self):
        from xsm import config, paths
        a, _ = self._dirs()
        with self.assertRaises(ValueError):
            config.join("../x", a)
        paths.write_json(paths.path(config.CONFIG), {"scopes": [
            {"id": "manual", "members": [{"cwd": "/somewhere/*"}]}]}, mode=0o644)
        with self.assertRaises(ValueError):
            config.join("manual", a)


class DefaultHomePrefixTest(TempState):
    """Whether the installing shell exports XSM_HOME=~/.xsm must not change the
    hook command, and an older install carrying that prefix is kept as is."""

    def test_default_home_is_not_written_and_old_prefix_is_kept(self):
        from unittest import mock
        from xsm import install
        with mock.patch.dict(os.environ, {"XSM_HOME": os.path.expanduser("~/.xsm")}):
            plain = install.hook_command("claude", "SessionStart")
        self.assertFalse(plain.startswith("XSM_HOME="))
        old = "XSM_HOME=%s %s" % (os.path.expanduser("~/.xsm"), plain)
        self.assertTrue(install._same_command(old, plain))
        self.assertFalse(install._same_command("XSM_HOME=/elsewhere " + plain, plain))


class CommandInstallTest(TempState):
    """Slash commands and the skill go in with the hooks, carry an absolute
    launcher path so they never depend on PATH, and come out again without
    touching anything we did not write."""

    def _home(self):
        home = os.path.join(self.tmp, "claude-cmd")
        os.makedirs(home, exist_ok=True)
        return home

    def test_commands_are_written_with_an_absolute_launcher(self):
        from xsm import install
        home = self._home()
        written = install.install_commands(home)
        self.assertTrue(written)
        body = open(os.path.join(home, "commands", "xsm-list.md")).read()
        self.assertIn(install.launcher(), body)
        self.assertNotIn("{{XSM}}", body)
        self.assertIn(install.FILE_MARKER, body)

    def test_the_skill_is_linked_and_unlinked(self):
        from xsm import install
        home = self._home()
        self.assertEqual(install.install_skill(home)[0], "linked")
        link = os.path.join(home, "skills", "xsm")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(install.install_skill(home)[0], "linked")      # idempotent
        self.assertTrue(install.remove_skill(home))
        self.assertFalse(os.path.exists(link))

    def test_a_foreign_command_of_the_same_name_survives(self):
        from xsm import install
        home = self._home()
        os.makedirs(os.path.join(home, "commands"), exist_ok=True)
        target = os.path.join(home, "commands", "xsm-list.md")
        open(target, "w").write("---\ndescription: mine\n---\nkeep me\n")
        install.install_commands(home)
        self.assertEqual(open(target).read(), "---\ndescription: mine\n---\nkeep me\n")
        install.remove_commands(home)
        self.assertTrue(os.path.exists(target))

    def test_codex_gets_each_command_as_a_skill_it_never_calls_itself(self):
        """Codex has no slash commands; `$xsm` worked and `$xsm-list` did not
        exist (2026-09-22). Each command becomes a skill of the same name."""
        from xsm import install
        home = os.path.join(self.tmp, "codex-cmd")
        written = install.install_codex_commands(home)
        self.assertEqual(sorted(os.path.basename(w) for w in written),
                         sorted(os.path.basename(f)[:-3] for f in install.command_files()))
        skill = open(os.path.join(home, "skills", "xsm-list", "SKILL.md")).read()
        self.assertIn("name: xsm-list\n", skill)
        self.assertIn(install.launcher() + " list --table", skill,
                      "Codex does not run !`cmd`; the model is told to")
        self.assertNotIn("!`", skill)
        self.assertIn("not in a code block", skill, "a table is left for the TUI to draw")
        self.assertNotIn("{{XSM}}", skill)
        policy = open(os.path.join(home, "skills", "xsm-list", "agents", "openai.yaml")).read()
        self.assertIn("allow_implicit_invocation: false", policy)
        send = open(os.path.join(home, "skills", "xsm-send", "SKILL.md")).read()
        self.assertIn("$xsm-send", send)
        self.assertNotIn("$ARGUMENTS", send)
        self.assertIn("xsm_send", send, "the sandbox route is spelled out")
        self.assertEqual(install.remove_codex_commands(home), len(written))
        self.assertFalse(os.path.exists(os.path.join(home, "skills", "xsm-list")))

    def test_a_foreign_codex_skill_of_the_same_name_survives(self):
        from xsm import install
        home = os.path.join(self.tmp, "codex-cmd")
        mine = os.path.join(home, "skills", "xsm-list")
        os.makedirs(mine)
        open(os.path.join(mine, "SKILL.md"), "w").write("---\nname: xsm-list\n---\nmine\n")
        install.install_codex_commands(home)
        install.remove_codex_commands(home)
        self.assertEqual(open(os.path.join(mine, "SKILL.md")).read(),
                         "---\nname: xsm-list\n---\nmine\n")

    def test_a_foreign_skill_directory_is_left_alone(self):
        from xsm import install
        home = self._home()
        existing = os.path.join(home, "skills", "xsm")
        os.makedirs(existing)
        self.assertEqual(install.install_skill(home)[0], "foreign")
        self.assertFalse(install.remove_skill(home))
        self.assertTrue(os.path.isdir(existing))

    def test_a_copy_is_reported_as_current_or_stale(self):
        """Following an older instruction left people with a copied SKILL.md;
        install has to say when that copy has fallen behind."""
        from xsm import install
        home = self._home()
        skill_dir = os.path.join(home, "skills", "xsm")
        os.makedirs(skill_dir)
        ours = os.path.join(install.REPO, "skills", "xsm", "SKILL.md")
        shutil.copy2(ours, os.path.join(skill_dir, "SKILL.md"))
        self.assertEqual(install.skill_state(home)[0], "copy-current")
        open(os.path.join(skill_dir, "SKILL.md"), "a").write("\nstale line\n")
        state, detail = install.skill_state(home)
        self.assertEqual(state, "copy-stale")
        self.assertTrue(detail.endswith("SKILL.md"))

    def test_a_link_nested_inside_the_directory_is_reported(self):
        """`ln -sfn repo/skills/xsm <home>/skills/xsm` puts the link *inside* an
        existing directory; the old instructions did exactly that."""
        from xsm import install
        home = self._home()
        skill_dir = os.path.join(home, "skills", "xsm")
        os.makedirs(skill_dir)
        os.symlink(os.path.join(install.REPO, "skills", "xsm"), os.path.join(skill_dir, "xsm"))
        state, detail = install.skill_state(home)
        self.assertEqual(state, "nested-link")
        self.assertTrue(detail.endswith("xsm/xsm"))


class StatuslineTest(TempState):
    """The statusLine path calls no model, so it must be cheap, read the session
    from stdin, and never replace a statusLine the user already has."""

    def _home(self, settings=None):
        from xsm import paths
        home = os.path.join(self.tmp, "claude-sl")
        os.makedirs(home, exist_ok=True)
        paths.write_json(os.path.join(home, "settings.json"), settings or {}, mode=0o644)
        return home

    def test_output_names_this_session_from_stdin(self):
        from xsm import registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="worker")
        env = dict(os.environ, XSM_HOME=self.tmp)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        out = subprocess.run([os.path.join(REPO, "bin", "xsm"), "statusline"],
                             input='{"session_id": "t1"}', capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("as worker", out.stdout)
        self.assertIn("0 peers", out.stdout)

    def test_install_sets_it_and_uninstall_removes_it(self):
        from xsm import install, paths
        home = self._home({"model": "opus"})
        self.assertEqual(install.install_statusline(home), "installed")
        self.assertEqual(install.install_statusline(home), "already")
        self.assertIn("statusline", paths.read_json(os.path.join(home, "settings.json"))["statusLine"]["command"])
        self.assertTrue(install.remove_statusline(home))
        self.assertEqual(paths.read_json(os.path.join(home, "settings.json")), {"model": "opus"})

    def test_an_existing_statusline_is_kept_and_xsm_adds_one_line_under_it(self):
        """A dashboard, Orca's line, anything: its output goes out unchanged and
        xsm adds exactly one line; uninstall gives the original back."""
        from xsm import install, paths
        script = os.path.join(self.tmp, "mine.sh")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\nread x\necho \"DASH line1\"\necho \"DASH line2 $x\"\n")
        os.chmod(script, 0o755)
        mine = {"type": "command", "command": script, "refreshInterval": 7, "padding": 1}
        home = self._home({"statusLine": mine})
        self.assertEqual(install.install_statusline(home), "composed")
        now = paths.read_json(os.path.join(home, "settings.json"))["statusLine"]
        self.assertIn("--base", now["command"])
        self.assertEqual((now["refreshInterval"], now["padding"]), (7, 1), "its settings carry over")
        self.assertEqual(install.install_statusline(home), "already")
        env = dict(os.environ, XSM_HOME=self.tmp)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        out = subprocess.run(["/bin/sh", "-c", now["command"].split(" #")[0]],
                             input='{"session_id": "t1"}', capture_output=True, text=True, env=env)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[:2], ["DASH line1", 'DASH line2 {"session_id": "t1"}'],
                         "the user's statusline gets the same input and prints unchanged")
        self.assertEqual(len(lines), 3, "xsm adds one line, no more")
        self.assertTrue(lines[2].startswith("xsm "))
        self.assertTrue(install.remove_statusline(home))
        self.assertEqual(paths.read_json(os.path.join(home, "settings.json"))["statusLine"], mine)

    def test_a_broken_base_statusline_does_not_take_xsms_line_with_it(self):
        from xsm import install, paths
        home = self._home({"statusLine": {"type": "command", "command": "exit 3"}})
        install.install_statusline(home)
        now = paths.read_json(os.path.join(home, "settings.json"))["statusLine"]
        env = dict(os.environ, XSM_HOME=self.tmp)
        out = subprocess.run(["/bin/sh", "-c", now["command"].split(" #")[0]], input="{}",
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0)
        self.assertTrue(out.stdout.startswith("xsm "))


class CompactOutputTest(TempState):
    """Slash commands have the model copy the CLI output back verbatim, so the
    output is the cost: no alignment padding, home shortened, unregistered and
    stopped sessions left out."""

    def test_list_compact_is_short_and_unpadded(self):
        from xsm import cli, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="worker")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--compact", "--dir", self.tmp])
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].endswith("(here)"), lines)
        self.assertTrue(lines[1].startswith(" worker@codex ["), lines)
        self.assertNotIn(self.tmp, lines[1], "the folder is said once, above its sessions")
        self.assertFalse(any("  " in line for line in lines))

    def test_list_table_is_markdown_the_folder_said_once(self):
        """For the TUI to draw: both Claude Code and Codex render tables."""
        from xsm import cli, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="one|two")
        registry.upsert("codex", home, "t2", os.getpid(), self.tmp, name="three")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--table", "--dir", self.tmp])
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "| folder | session | runtime | ref | note |")
        self.assertEqual(lines[1], "|---|---|---|---|---|")
        self.assertEqual([l.split("|")[1].strip() for l in lines[2:]], ["here", ""])
        self.assertIn("one\\|two", out.getvalue(), "a pipe in a name does not split a cell")

    def test_every_display_command_asks_for_a_table_its_output_provides(self):
        """/xsm-list, /xsm-who, /xsm-inbox, /xsm-projects, /xsm-doctor all pass
        Markdown tables through for the TUI to draw."""
        import re
        from unittest import mock
        from xsm import cli, ledger, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        me = registry.upsert("codex", home, "t1", os.getpid(), self.tmp, name="me")
        ledger.queued("m1", me, me, "dir:x", "note", "a | b")
        for name in ("xsm-list", "xsm-who", "xsm-inbox", "xsm-projects", "xsm-doctor"):
            body = open(os.path.join(REPO, "commands", name + ".md")).read()
            self.assertIn("not in a\ncode block", body, name)
            for command in re.findall(r"!`\{\{XSM\}\} ([^`]*)`", body):
                self.assertIn("--table", command, name)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        mock.patch.object(registry, "me", lambda: me):
                    cli.main(command.split())
                text = out.getvalue()
                self.assertTrue(text.startswith("| ") or text.startswith("no ")
                                or text.startswith("nothing"), (name, text[:80]))
                self.assertNotIn("```", text)
        self.assertIn("a \\| b", _run_cli(["ledger", "--table"]), "a pipe stays inside its cell")

    def test_list_compact_groups_by_folder_this_one_first(self):
        """A path on every line wrapped each entry in a narrow Codex pane."""
        from xsm import cli, registry
        home = os.path.join(self.tmp, "homes", "codex")
        sub_dir, other = os.path.join(self.tmp, "a", "b"), os.path.join(self.tmp, "..", "zz-other")
        for d in (home, sub_dir):
            os.makedirs(d, exist_ok=True)
        registry.upsert("codex", home, "t1", os.getpid(), sub_dir, name="deep")
        registry.upsert("codex", home, "t2", os.getpid(), self.tmp, name="one")
        registry.upsert("codex", home, "t3", os.getpid(), self.tmp, name="two")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--compact", "--dir", self.tmp, "-a"])
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].endswith("(here)"), lines)
        self.assertEqual(sorted(l.split("@")[0].strip() for l in lines[1:3]), ["one", "two"])
        self.assertEqual(lines[3], "./a/b")
        self.assertTrue(lines[4].startswith(" deep@codex"), lines)


class ListScopeTest(TempState):
    """`xsm list` shows the sessions this folder can talk to; -a shows all."""

    def _list(self, *argv):
        from xsm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--compact"] + list(argv))
        return out.getvalue()

    def test_default_is_this_project_and_a_shows_everything(self):
        from xsm import config, registry
        here, there = os.path.join(self.tmp, "here"), os.path.join(self.tmp, "there")
        os.makedirs(here)
        os.makedirs(there)
        home = os.path.join(self.tmp, "codex")
        os.makedirs(home)
        registry.upsert("codex", home, "t1", os.getpid(), here, name="near")
        registry.upsert("codex", home, "t2", os.getpid(), there, name="far")
        out = self._list("--dir", here)
        self.assertIn("near@", out)
        self.assertNotIn("far@", out)
        self.assertIn("far@", self._list("--dir", here, "-a"))
        config.join("demo", here)
        config.join("demo", there)
        self.assertIn("far@", self._list("--dir", here), "a joined project counts as this one")


class SupersededSessionTest(TempState):
    def test_an_id_the_process_no_longer_runs_is_ended(self):
        from xsm import registry
        registry.identity.socket_live = lambda path: True
        registry._claude_native = lambda home, pid: {"name": "b", "socket": "/s",
                                                    "name_source": "user",
                                                    "native_session_id": "current"}
        registry.upsert("claude", self.tmp, "current", os.getpid(), self.tmp)
        registry.upsert("claude", self.tmp, "old", os.getpid(), self.tmp)
        states = {r["session_id"]: r["state"] for r in registry.records()}
        self.assertEqual(states, {"current": "live", "old": "ended"})

    def _ledger_line(self):
        from xsm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["ledger", "--compact"])
        return out.getvalue().strip()

    def test_ledger_compact_has_one_line_per_message(self):
        from xsm import ledger, registry
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home, exist_ok=True)
        b = registry.upsert("codex", home, "t-b", os.getpid(), self.tmp, name="b")   # live target
        a = {"name": "a", "alias": "h", "ref": "r1", "runtime": "claude"}
        ledger.queued("m1", a, b, "dir:x", "note", "hello\nworld")
        self.assertEqual(self._ledger_line(), "queued a->b m1: hello world")

    def test_queued_to_a_stopped_target_reads_undelivered(self):
        """It will never be recorded as delivered; do not leave it looking pending."""
        from xsm import ledger
        a = {"name": "a", "alias": "h", "ref": "r1", "runtime": "claude"}
        gone = {"name": "b", "alias": "h", "ref": "r2", "runtime": "claude"}
        ledger.queued("m1", a, gone, "dir:x", "note", "hello")
        self.assertTrue(self._ledger_line().startswith("undelivered a->b m1"))

    def test_display_commands_ask_for_a_verbatim_copy(self):
        """The prompts must not contain conditions for the model to weigh."""
        for name in ("xsm-list", "xsm-who", "xsm-inbox", "xsm-doctor"):
            body = open(os.path.join(REPO, "commands", name + ".md")).read()
            self.assertIn("copied exactly", body, name)
            self.assertNotIn("unless", body, name)


class SelfIdentityTest(TempState):
    """A session asking who it is, before its own hook has ever run.

    Measured 2026-09-22: xsm was installed into a home whose session was
    already open, and /xsm-who — whose shell runs before that prompt's hook —
    said "not registered". The next prompt registered it; the command was just
    too early."""

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "claude-home")
        os.makedirs(os.path.join(self.home, "sessions"))
        for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_MESSAGING_SOCKET"):
            self.addCleanup(lambda k=key, v=os.environ.get(key):
                            os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None))
        os.environ.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)
        os.environ["CLAUDE_CONFIG_DIR"] = self.home
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s-mine"
        paths_json = os.path.join(self.home, "sessions", "%d.json" % os.getpid())
        from xsm import paths
        paths.write_json(paths_json, {"pid": os.getpid(), "sessionId": "s-mine", "cwd": self.tmp,
                                      "name": "mine", "nameSource": "auto"})

    def _who(self):
        from xsm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["who"])
        return code, out.getvalue(), err.getvalue()

    def test_installed_but_not_yet_hooked_is_adopted(self):
        from xsm import install, registry
        install.apply(self.home, "claude")
        me = registry.me()
        self.assertIsNotNone(me, "xsm is in this session's own settings: that is the consent")
        self.assertEqual(me["session_id"], "s-mine")
        self.assertTrue(me.get("adopted"))
        code, out, _ = self._who()
        self.assertEqual(code, 0)
        self.assertIn("mine@", out)
        # The real hook, at the next prompt, takes over the record.
        registry.upsert("claude", self.home, "s-mine", os.getpid(), self.tmp)
        self.assertNotIn("adopted", registry.me())

    def test_a_home_without_xsm_is_not_adopted_and_says_how_to_fix_it(self):
        from xsm import registry
        self.assertIsNone(registry.me())
        code, _, err = self._who()
        self.assertEqual(code, 2)
        self.assertIn("xsm install --claude-home", err)

    def test_hooks_removed_from_a_declared_home_is_not_consent(self):
        from xsm import install, registry
        install.apply(self.home, "claude")
        install.remove(self.home, "claude")
        self.assertIsNone(registry.me())
        reason = registry.self_consent(registry.claude_home_here())
        assert reason is not None, "hooks were removed, so there must be a reason"
        self.assertIn("missing", reason)

    def test_a_sandboxed_codex_session_finds_itself_by_thread_id(self):
        """The Codex sandbox refuses `ps`, so the process walk cannot identify a
        session there; two sharing a folder were each refused (S10 smoke run)."""
        from xsm import identity, registry
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.addCleanup(lambda v=os.environ.get("CODEX_THREAD_ID"):
                        os.environ.__setitem__("CODEX_THREAD_ID", v) if v is not None
                        else os.environ.pop("CODEX_THREAD_ID", None))
        home = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(home)
        registry.upsert("codex", home, "t-one", os.getpid(), self.tmp, name="one")
        registry.upsert("codex", home, "t-two", os.getpid(), self.tmp, name="two")
        identity.ancestor_pid = lambda names, max_hops=10: None      # what the sandbox does
        os.environ["CODEX_THREAD_ID"] = "t-two"
        self.assertEqual(registry.me()["session_id"], "t-two")
        os.environ["CODEX_THREAD_ID"] = "t-one"
        self.assertEqual(registry.me()["session_id"], "t-one")

    def test_a_sandbox_that_forbids_signals_and_ps_still_sees_live_peers(self):
        """From inside the Codex sandbox kill(pid, 0) answers EPERM and `ps`
        cannot run. Both were read as "dead", so `xsm list` there showed no one
        (S10 pilot, seen in xsm's own spans)."""
        from unittest import mock
        from xsm import identity
        rec = {"runtime": "codex", "pid": os.getpid(), "lstart": "Tue Sep 22 11:06:20 2026"}
        with mock.patch("os.kill", side_effect=PermissionError(1, "Operation not permitted")), \
                mock.patch.object(identity, "lstart", lambda pid: None):
            self.assertEqual(identity.state_of(rec), "live")
        with mock.patch("os.kill", side_effect=ProcessLookupError(3, "No such process")):
            self.assertEqual(identity.state_of(rec), "stale", "a pid that is gone is still gone")
        with mock.patch.object(identity, "lstart", lambda pid: "Mon Sep 21 09:00:00 2026"):
            self.assertEqual(identity.state_of(rec), "stale", "a measured, different start is reuse")

    def test_a_sandbox_that_forbids_the_inbox_socket_still_sees_live_claude_peers(self):
        """The Codex sandbox refuses connect() on a Claude inbox socket with
        EPERM; that read as a dead socket, and a Codex worker listed both its
        Claude peers as `stale` for a whole run (S10 collab run 4)."""
        import socket
        from unittest import mock
        from xsm import identity
        rec = {"runtime": "claude", "pid": os.getpid(), "socket": "/tmp/cc-socks/1.sock"}
        with mock.patch.object(socket.socket, "connect",
                               side_effect=PermissionError(1, "Operation not permitted")):
            self.assertEqual(identity.state_of(rec), "live")
        with mock.patch.object(socket.socket, "connect",
                               side_effect=ConnectionRefusedError(61, "Connection refused")):
            self.assertEqual(identity.state_of(rec), "stale", "a socket nobody listens on is gone")

    def test_an_unregistered_session_never_borrows_a_neighbours_identity(self):
        """Before: a session that knew its own id but had no record fell through
        to the cwd guess and printed whichever session shared its folder."""
        from xsm import registry
        other = os.path.join(self.tmp, "homes", "codex")
        os.makedirs(other)
        registry.upsert("codex", other, "t-neighbour", os.getpid(), self.tmp, name="neighbour")
        self.assertIsNone(registry.me(), "not someone else's record")


class LedgerFailureTest(TempState):
    def test_a_send_the_sandbox_blocked_is_not_left_as_queued(self):
        """Five such sends sat as `queued` in the S10 collab pilot; the failure
        was only in the decision log."""
        from xsm import ledger
        ledger.queued("m1", {"name": "a"}, {"name": "b"}, "repo:x", "note", "hi")
        ledger.failed("m1", "sandbox-blocked: cannot open the inbox socket")
        entry = ledger.status("m1")
        self.assertEqual(entry["status"], "error")
        self.assertIn("sandbox-blocked", entry["error"])


class LifecycleTest(TempState):
    """What happens to a session after it stops (ADR-0001 addendum)."""

    def _register(self, name="s", runtime="codex", pid=None):
        from xsm import registry
        home = os.path.join(self.tmp, "homes", runtime)
        os.makedirs(home, exist_ok=True)
        return registry.upsert(runtime, home, "sid-" + name, pid or os.getpid(), self.tmp, name=name)

    def _dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def test_clean_exit_reads_ended_and_a_crash_reads_stale(self):
        from xsm import identity, registry
        clean = self._register("clean", pid=self._dead_pid())
        registry.mark_ended("codex", clean["session_id"], "prompt_input_exit")
        crashed = self._register("crashed", pid=self._dead_pid())
        self.assertEqual(identity.state_of(registry.by_session("codex", clean["session_id"])), "ended")
        self.assertEqual(identity.state_of(registry.by_session("codex", crashed["session_id"])), "stale")

    def test_resuming_clears_the_goodbye(self):
        """claude --resume comes back with the same session id (measured)."""
        from xsm import identity, registry
        rec = self._register("resumable", pid=self._dead_pid())
        registry.mark_ended("codex", rec["session_id"], "prompt_input_exit")
        again = self._register("resumable")                     # same id, live pid
        self.assertNotIn("ended_at", again)
        self.assertEqual(identity.state_of(again), "live")
        self.assertEqual(again["ref"], rec["ref"])

    def test_sessionend_hook_marks_the_pointer(self):
        from xsm import receive, registry
        rec = self._register("leaving")
        receive.handle({"hook_event_name": "SessionEnd", "session_id": rec["session_id"],
                        "reason": "prompt_input_exit", "turn_id": "x"})
        self.assertEqual(registry.by_session("codex", rec["session_id"])["end_reason"],
                         "prompt_input_exit")

    def test_prune_keeps_recent_stopped_pointers_and_drops_old_ones(self):
        from xsm import housekeeping, paths, registry
        live = self._register("live")
        recent = self._register("recent", pid=self._dead_pid())
        old = self._register("old", pid=self._dead_pid())
        p = os.path.join(self.tmp, "sessions", "codex-%s.json" % old["session_id"])
        rec = paths.read_json(p)
        rec["updated"] = time.time() - 30 * 86400
        paths.write_json(p, rec)
        removed = housekeeping.prune()
        self.assertEqual(removed["sessions"], ["old"])
        names = {r["name"] for r in registry.records()}
        self.assertEqual(names, {"live", "recent"})

    def test_prune_ages_out_ledger_and_held(self):
        from xsm import housekeeping, paths
        now = time.time()
        paths.write_json(paths.path(paths.LEDGER, "fresh.json"), {"t": now})
        paths.write_json(paths.path(paths.LEDGER, "ancient.json"), {"t": now - 90 * 86400})
        paths.write_json(paths.path(paths.HELD, "ancient.json"), {"t": now - 90 * 86400})
        removed = housekeeping.prune()
        self.assertEqual(removed["ledger"], ["ancient.json"])
        self.assertEqual(removed["held"], ["ancient.json"])
        self.assertTrue(os.path.exists(paths.path(paths.LEDGER, "fresh.json")))

    def test_opportunistic_prune_runs_at_most_hourly(self):
        from xsm import housekeeping
        self.assertIsNotNone(housekeeping.maybe_prune())
        self.assertIsNone(housekeeping.maybe_prune())

    def test_a_stopped_target_comes_with_a_resume_command(self):
        from xsm import registry, resolve
        rec = self._register("sleeper", pid=self._dead_pid())
        registry.mark_ended("codex", rec["session_id"], "prompt_input_exit")
        found = resolve.resolve("sleeper")
        self.assertEqual(found.status, "offline-only")
        self.assertIn("codex resume sid-sleeper", found.reason)
        self.assertIn("exited cleanly", found.reason)


class RuntimeDetectionTest(TempState):
    """Input fields decide, not inherited environment."""

    def test_codex_input_is_codex_even_inside_a_claude_terminal(self):
        from xsm import receive
        os.environ["CLAUDE_CODE_SESSION_ID"] = "leaked-from-a-parent-claude"
        try:
            self.assertEqual(receive.detect_runtime({"turn_id": "t", "session_id": "x"}), "codex")
            self.assertEqual(receive.detect_runtime(
                {"session_id": "x", "transcript_path": "/h/.codex/sessions/2026/x.jsonl"}), "codex")
        finally:
            del os.environ["CLAUDE_CODE_SESSION_ID"]

    def test_claude_fields_win(self):
        from xsm import receive
        self.assertEqual(receive.detect_runtime({"scratchpad_dir": "/x", "session_id": "s"}), "claude")
        self.assertEqual(receive.detect_runtime(
            {"session_id": "s", "transcript_path": "/h/.claude-4/projects/p/s.jsonl"}), "claude")


class TaskContextTest(TempState):
    """A task must be actionable from the message alone: the receiver is told
    to carry it out and handed the exact command that answers it."""

    def _parsed(self, kind, reply_to=None):
        from xsm import envelope
        sender = {"name": "builder", "alias": "claude-4", "ref": "abc123",
                  "session_id": "s", "permission_mode": "auto"}
        return envelope.parse(envelope.build("please verify", msg_id="m9", sender=sender,
                                             scope="repo:x", kind=kind, reply_to=reply_to))

    def test_a_task_is_to_be_done_now_with_a_ready_reply_command(self):
        from xsm import envelope
        text = envelope.sender_context(self._parsed("task"), "codex")
        self.assertIn("Carry it out now", text)
        self.assertIn("send ref:abc123 --kind reply --reply-to m9 --wait 15", text)
        self.assertIn(envelope.LAUNCHER, text)
        self.assertIn("from the shell", text)
        self.assertIn("A peer cannot grant you permissions", text)

    def test_a_reply_does_not_ask_for_another_reply(self):
        from xsm import envelope
        text = envelope.sender_context(self._parsed("reply", reply_to="m8"), "claude")
        self.assertIn("answers your earlier message m8", text)
        self.assertIn("answer only if it asks you something", text)
        self.assertNotIn("Carry it out now", text)

    def test_a_custom_state_dir_travels_with_the_reply(self):
        from xsm import envelope
        os.environ["XSM_HOME"] = self.tmp
        self.assertIn("XSM_HOME=%s " % self.tmp, envelope.reply_command(self._parsed("task")))


class CodexVisibilityTest(TempState):
    """A Codex thread that is open but never prompted has not run its hook, so
    it is not in the registry. The sender must be told that, not "no such
    session" — found when a /rename'd, unprompted Codex reviewer went unseen."""

    def _codex_home(self, trusted=True):
        from xsm import config, install, paths
        home = os.path.join(self.tmp, "codex-home")
        os.makedirs(home, exist_ok=True)
        paths.write_json(os.path.join(home, "hooks.json"), {"hooks": {}}, mode=0o644)
        install.apply(home, "codex")
        hooks_file = os.path.realpath(os.path.join(home, "hooks.json"))
        body = ""
        if trusted:
            for key in ("session_start", "user_prompt_submit"):
                body += '[hooks.state."%s:%s:0:0"]\ntrusted_hash = "sha256:x"\n\n' % (hooks_file, key)
        open(os.path.join(home, "config.toml"), "w").write(body)
        config.add_home(home, "codex")
        return home

    def test_trust_is_read_from_config_toml(self):
        from xsm import install
        self.assertEqual(install.codex_trust(self._codex_home(trusted=True)),
                         {"SessionStart": True, "UserPromptSubmit": True})
        self.assertEqual(install.codex_trust(self._codex_home(trusted=False)),
                         {"SessionStart": False, "UserPromptSubmit": False})

    def _with_open_thread(self, home, rollout=None):
        from xsm import registry
        registry._open_codex_threads = lambda h: [("t-open", "reviewer", self.tmp, rollout, 0, 0, os.getpid())]

    def test_unprompted_thread_is_reported_with_the_reason(self):
        from xsm import resolve
        home = self._codex_home(trusted=True)
        self._with_open_thread(home)
        found = resolve.resolve("reviewer")
        self.assertEqual(found.status, "unregistered")
        self.assertIn("no prompt yet", found.reason)

    def test_untrusted_hooks_are_named_as_the_cause(self):
        from xsm import resolve
        home = self._codex_home(trusted=False)
        self._with_open_thread(home)
        self.assertIn("not trusted", resolve.resolve("reviewer").reason)


class AdoptionTest(TempState):
    """An open Codex thread in a home whose xsm hooks are trusted is registered
    by the CLI before its first prompt, so it can be handed its first task.
    Measured: a never-prompted thread took a task through the queue, and its
    own hook registered it on that first turn."""

    def _home(self, trusted):
        from xsm import config, install, paths
        home = os.path.join(self.tmp, "codex-adopt")
        os.makedirs(home, exist_ok=True)
        paths.write_json(os.path.join(home, "hooks.json"), {"hooks": {}}, mode=0o644)
        install.apply(home, "codex")
        hooks_file = os.path.realpath(os.path.join(home, "hooks.json"))
        body = "".join('[hooks.state."%s:%s:0:0"]\ntrusted_hash = "sha256:x"\n\n' % (hooks_file, k)
                       for k in ("session_start", "user_prompt_submit")) if trusted else ""
        open(os.path.join(home, "config.toml"), "w").write(body)
        config.add_home(home, "codex")
        return home

    def _open_thread(self):
        from xsm import registry
        registry._open_codex_threads = lambda h: [
            ("t-new", "fresh", self.tmp, None, 0, 0, os.getpid())]

    def test_trusted_home_is_adopted(self):
        from xsm import registry, resolve
        self._home(trusted=True)
        self._open_thread()
        adopted = registry.adopt_open_codex()
        self.assertEqual([r["name"] for r in adopted], ["fresh"])
        self.assertEqual(resolve.resolve("fresh").status, "resolved")

    def test_untrusted_home_is_not_adopted(self):
        from xsm import registry
        self._home(trusted=False)
        self._open_thread()
        self.assertEqual(registry.adopt_open_codex(), [])

    def test_the_hook_takes_over_the_pointer(self):
        from xsm import registry
        home = self._home(trusted=True)
        self._open_thread()
        registry.adopt_open_codex()
        again = registry.upsert("codex", home, "t-new", os.getpid(), self.tmp, permission_mode="auto")
        self.assertNotIn("adopted", again)


if __name__ == "__main__":
    unittest.main()


class ListClearTest(TempState):
    def test_clear_forgets_stopped_sessions_only(self):
        from xsm import cli, registry
        here = os.path.join(self.tmp, "here")
        there = os.path.join(self.tmp, "there")
        os.makedirs(here)
        os.makedirs(there)
        home = os.path.join(self.tmp, "codex")
        os.makedirs(home)
        registry.upsert("codex", home, "live1", os.getpid(), here, name="alive")
        for sid, cwd in (("dead1", here), ("dead2", there)):
            rec = registry.upsert("codex", home, sid, 999999, cwd, name=sid)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "clear", "--dir", here])
        self.assertIn("cleared 1", out.getvalue())
        left = {r["session_id"] for r in registry.records()}
        self.assertEqual(left, {"live1", "dead2"}, "live kept; other project untouched")
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["list", "clear", "-a", "--dir", here])
        self.assertEqual({r["session_id"] for r in registry.records()}, {"live1"})
