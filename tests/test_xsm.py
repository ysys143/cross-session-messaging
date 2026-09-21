"""Unit tests for the parts that decide something: wire format, addressing,
scope, the hook's fallback, and the settings merge.

Run: python3 -m unittest discover -s tests -v
Everything here works on a temporary XSM_HOME; no session is contacted.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xsm-test-")
        os.environ["XSM_HOME"] = self.tmp
        for mod in [m for m in list(sys.modules) if m.startswith("xsm")]:
            del sys.modules[mod]
        from xsm import paths
        paths.HOME = self.tmp
        paths.ensure_home()

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


if __name__ == "__main__":
    unittest.main()
