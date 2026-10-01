"""Messaging is never stopped by a permission (user decision, 2026-10-01): the
allow list holds the messaging commands and tools, and each Claude home says
crossSessionInbound "accept" unless its person said otherwise."""
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
REPO = test_xsm.REPO


def _run_cli(*argv):
    from xsm import cli
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue() + err.getvalue()


class AllowMessagingTest(TempState):
    def _home(self, name="claude-allow", settings=None):
        from xsm import paths
        home = os.path.join(self.tmp, name)
        os.makedirs(home)
        if settings is not None:
            paths.write_json(os.path.join(home, "settings.json"), settings)
        return home

    def test_the_messaging_commands_and_tools_are_allowed_by_name_and_by_path(self):
        from xsm import install, paths
        home = self._home(settings={"permissions": {"allow": ["Bash(ls:*)"]}})
        self.assertEqual(install.allow_form_tools(home), "added")
        allow = paths.read_json(os.path.join(home, "settings.json"))["permissions"]["allow"]
        for command in ("send", "inbox", "list", "who", "held", "ledger", "status", "doctor",
                        "--version"):
            self.assertIn("Bash(xsm %s:*)" % command, allow)
            self.assertIn("Bash(%s %s:*)" % (os.path.join(install.runtime_root(), "bin", "xsm"),
                                              command), allow)
        for prefix in ("mcp__xsm__", "mcp__plugin_xsm_xsm__"):
            for tool in ("xsm_send", "xsm_inbox", "xsm_post", "xsm_channel"):
                self.assertIn(prefix + tool, allow)
        for command in ("spawn", "post", "doc add", "install", "uninstall", "stop"):
            self.assertNotIn("Bash(xsm %s:*)" % command, allow, "still the classifier's")

    def test_a_plugin_homes_path_is_allowed_too_and_a_replaced_one_goes(self):
        from xsm import install, paths
        home = self._home("claude-plugin-allow")
        cache = os.path.join(home, "plugins", "cache", "xsm", "xsm", "0.4.15")
        os.makedirs(cache)
        paths.write_json(os.path.join(home, "plugins", "installed_plugins.json"),
                         {"version": 2, "plugins": {"xsm@xsm": [
                             {"version": "0.4.15", "installPath": cache}]}})
        install.allow_form_tools(home)
        allow = paths.read_json(os.path.join(home, "settings.json"))["permissions"]["allow"]
        self.assertIn("Bash(%s send:*)" % os.path.join(cache, "bin", "xsm"), allow)
        # An update replaces the folder: the old path's rules were xsm's and go.
        newer = os.path.join(home, "plugins", "cache", "xsm", "xsm", "0.4.16")
        os.makedirs(newer)
        paths.write_json(os.path.join(home, "plugins", "installed_plugins.json"),
                         {"version": 2, "plugins": {"xsm@xsm": [
                             {"version": "0.4.16", "installPath": newer}]}})
        self.assertEqual(install.allow_form_tools(home), "added")
        allow = paths.read_json(os.path.join(home, "settings.json"))["permissions"]["allow"]
        self.assertNotIn("Bash(%s send:*)" % os.path.join(cache, "bin", "xsm"), allow)
        self.assertIn("Bash(%s send:*)" % os.path.join(newer, "bin", "xsm"), allow)

    def test_uninstall_removes_exactly_what_xsm_added_and_a_persons_own_rule_stays(self):
        from xsm import install, paths
        theirs = ["Bash(ls:*)", "Bash(xsm send:*)", "Bash(xsm who:*)"]
        home = self._home(settings={"permissions": {"allow": list(theirs), "deny": ["X"]}})
        target = os.path.join(home, "settings.json")
        self.assertEqual(install.allow_form_tools(home), "added")
        note = paths.read_json(install._state_file(install.ALLOWED, target))
        for rule in ("Bash(xsm send:*)", "Bash(xsm who:*)"):
            self.assertNotIn(rule, note["added"], "they were the person's")
        self.assertIn("Bash(xsm inbox:*)", note["added"])
        self.assertTrue(install.remove_form_tools(home))
        self.assertEqual(paths.read_json(target)["permissions"],
                         {"allow": theirs, "deny": ["X"]})

    def test_the_policy_off_adds_none_and_takes_back_what_it_added(self):
        from xsm import install, paths
        home = self._home()
        target = os.path.join(home, "settings.json")
        install.allow_form_tools(home)
        self.assertIn("Bash(xsm send:*)", paths.read_json(target)["permissions"]["allow"])
        with mock.patch.dict(os.environ, {"XSM_ALLOW_MESSAGING": "0"}):
            self.assertEqual(install.allow_form_tools(home), "updated")
            allow = paths.read_json(target)["permissions"]["allow"]
            self.assertNotIn("Bash(xsm send:*)", allow)
            self.assertNotIn("mcp__xsm__xsm_send", allow)
            self.assertIn("Bash(xsm link:*)", allow, "the approval rules stay")
            self.assertEqual(install.missing_form_tools(home), [])

    def test_doctor_names_a_home_without_them_and_says_the_agent_fixes_it(self):
        from xsm import config, install
        home = self._home("claude-doctor")
        config.add_home(home, "claude")
        install.allow_form_tools(home)
        with mock.patch.dict(os.environ, {"XSM_ALLOW_MESSAGING": "0"}):
            install.allow_form_tools(home)
        code, text = _run_cli("doctor")
        line = next((l for l in text.splitlines() if l.startswith("allow ")), "")
        self.assertIn("messaging commands and tools", line)
        self.assertIn("auto mode and default mode", line)
        self.assertIn("your agent can add them by running `xsm install --refresh`", line)

    def test_the_reply_command_is_the_plain_name_when_path_has_this_runtime(self):
        """So the one rule Bash(xsm send:*) covers it; else the absolute path."""
        from xsm import envelope
        parsed = envelope.parse(envelope.build(
            "hi", msg_id="m1", sender={"name": "a", "alias": "h", "ref": "aaaaaa"}, scope="s"))
        state = "XSM_HOME=%s " % self.tmp       # a test's state folder is not ~/.xsm
        with mock.patch.object(envelope.shutil, "which", return_value=envelope.LAUNCHER):
            self.assertTrue(envelope.reply_command(parsed, bare=True).startswith(
                state + "xsm send "))
            self.assertTrue(envelope.reply_command(parsed).startswith(
                state + envelope.LAUNCHER + " send "), "Codex: its shell may have no PATH")
            self.assertIn(" xsm send ", envelope.sender_context(parsed, "claude"))
            self.assertNotIn(envelope.LAUNCHER, envelope.sender_context(parsed, "claude"))
            self.assertIn(envelope.LAUNCHER, envelope.sender_context(parsed, "codex"))
        with mock.patch.object(envelope.shutil, "which", return_value="/usr/bin/xsm"):
            self.assertTrue(envelope.reply_command(parsed, bare=True).startswith(
                state + envelope.LAUNCHER + " send "), "an xsm that is another runtime's")
        with mock.patch.object(envelope.shutil, "which", return_value=None):
            self.assertIn(envelope.LAUNCHER, envelope.sender_context(parsed, "claude"))

    def test_the_skill_allows_xsm_send_and_stays_short(self):
        with open(os.path.join(REPO, "skills", "xsm", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        head = text.split("\n---\n", 1)[0]
        self.assertIn("Bash(xsm send:*)", head)
        self.assertLess(len(text.splitlines()), 90)


class InboundTest(TempState):
    def _home(self, settings=None):
        from xsm import paths
        home = os.path.join(self.tmp, "claude-inbound")
        os.makedirs(home, exist_ok=True)
        if settings is not None:
            paths.write_json(os.path.join(home, "settings.json"), settings)
        return home

    def test_it_is_set_once_noted_and_removed_only_by_the_one_that_set_it(self):
        from xsm import install, paths
        home = self._home({"model": "opus"})
        target = os.path.join(home, "settings.json")
        self.assertEqual(install.set_inbound(home), "set")
        self.assertEqual(paths.read_json(target), {"model": "opus", "crossSessionInbound": "accept"})
        self.assertEqual(install.set_inbound(home), "already")
        self.assertTrue(install.remove_inbound(home))
        self.assertEqual(paths.read_json(target), {"model": "opus"})
        self.assertFalse(install.remove_inbound(home))

    def test_a_value_the_home_already_has_is_the_persons_and_stays(self):
        from xsm import install, paths
        for value in ("hold", "refuse", "accept"):
            with self.subTest(value):
                home = self._home({"crossSessionInbound": value})
                target = os.path.join(home, "settings.json")
                self.assertEqual(install.set_inbound(home),
                                 "already" if value == "accept" else "kept:%s" % value)
                self.assertFalse(install.remove_inbound(home), "xsm did not set it")
                self.assertEqual(paths.read_json(target), {"crossSessionInbound": value})

    def test_a_value_changed_since_is_not_taken_out(self):
        from xsm import install, paths
        home = self._home({})
        target = os.path.join(home, "settings.json")
        install.set_inbound(home)
        paths.write_json(target, {"crossSessionInbound": "hold"})
        self.assertFalse(install.remove_inbound(home))
        self.assertEqual(paths.read_json(target), {"crossSessionInbound": "hold"})

    def test_the_policy_leave_sets_nothing(self):
        from xsm import install, paths
        home = self._home({})
        with mock.patch.dict(os.environ, {"XSM_CLAUDE_INBOUND": "leave"}):
            self.assertEqual(install.set_inbound(home), "off")
        self.assertEqual(paths.read_json(os.path.join(home, "settings.json")), {})

    def test_a_file_xsm_cannot_read_is_left_as_it_is(self):
        from xsm import install
        home = self._home()
        with open(os.path.join(home, "settings.json"), "w") as fh:
            fh.write("{ nope")
        self.assertEqual(install.set_inbound(home), "invalid")
        with open(os.path.join(home, "settings.json")) as fh:
            self.assertEqual(fh.read(), "{ nope")

    def test_install_refresh_and_uninstall_do_it_end_to_end_beside_the_allow_note(self):
        from xsm import install, paths
        home = self._home({"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]}})
        target = os.path.join(home, "settings.json")
        code, text = _run_cli("install", "--claude-home", home, "--no-mcp", "--python",
                              sys.executable)
        self.assertEqual(code, 0, text)
        self.assertIn('crossSessionInbound: set to "accept"', text)
        data = paths.read_json(target)
        self.assertEqual(data["crossSessionInbound"], "accept")
        self.assertIn("Bash(xsm send:*)", data["permissions"]["allow"])
        note = paths.read_json(install._state_file(install.ALLOWED, target))
        self.assertEqual(note["inbound"], "accept", "recorded, so uninstall takes out only it")
        code, text = _run_cli("install", "--refresh", "--no-mcp", "--python", sys.executable)
        self.assertNotIn("crossSessionInbound: set", text, "once")
        self.assertEqual(paths.read_json(install._state_file(install.ALLOWED, target))["inbound"],
                         "accept", "a refresh keeps the note's field")
        with mock.patch.object(install, "remove_mcp", return_value=False):   # never the real claude
            code, text = _run_cli("uninstall", "--claude-home", home)
        self.assertIn("removed the crossSessionInbound xsm set", text)
        self.assertEqual(paths.read_json(target), {"model": "opus", "hooks": {},
                                                   "permissions": {"allow": ["Bash(ls:*)"]}})

    def test_a_home_on_the_plugin_gets_it_on_refresh(self):
        from xsm import config, install, paths
        home = os.path.join(self.tmp, "claude-plugin-inbound")
        cache = os.path.join(home, "plugins", "cache", "xsm", "xsm", "0.4.3")
        os.makedirs(cache)
        paths.write_json(os.path.join(home, "plugins", "installed_plugins.json"),
                         {"version": 2, "plugins": {"xsm@xsm": [
                             {"version": "0.4.3", "installPath": cache}]}})
        config.add_home(home, "claude")
        code, text = _run_cli("install", "--refresh")
        self.assertIn('crossSessionInbound: set to "accept"', text)
        self.assertEqual(paths.read_json(os.path.join(home, "settings.json"))
                         ["crossSessionInbound"], "accept")

    def test_doctor_says_what_an_unset_or_held_home_means_and_who_fixes_it(self):
        from xsm import config
        home = self._home({})
        config.add_home(home, "claude")
        code, text = _run_cli("doctor")
        line = next(l for l in text.splitlines() if l.startswith("inbound "))
        self.assertIn("crossSessionInbound is not set", line)
        self.assertIn("your agent can run `xsm install --refresh`", line)
        self.assertNotIn("type", line)
        from xsm import paths
        paths.write_json(os.path.join(home, "settings.json"), {"crossSessionInbound": "hold"})
        line = next(l for l in _run_cli("doctor")[1].splitlines() if l.startswith("inbound "))
        self.assertIn('is "hold" (yours)', line)
        paths.write_json(os.path.join(home, "settings.json"), {"crossSessionInbound": "accept"})
        self.assertNotIn("inbound ", _run_cli("doctor")[1])

    def test_the_forecast_says_install_sets_it_and_who_runs_it(self):
        from xsm import send
        sender = {"permission_mode": "bypassPermissions"}
        target = {"runtime": "claude", "home": os.path.join(self.tmp, "nowhere"),
                  "permission_mode": "default"}
        verdict, why = send.native_forecast(sender, target)
        self.assertEqual(verdict, "hold")
        self.assertIn("`xsm install --refresh`", why)
        self.assertIn("your agent can run it", why)
        self.assertIn('crossSessionInbound to "accept"', why)


if __name__ == "__main__":
    unittest.main()
