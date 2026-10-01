"""A hook that cannot run is an error, never a block (2026-10-01).

Claude Code reads exit status 2 from a hook as "block", and Python exits 2 when
it cannot open the script. The terminal of one person lost macOS access to
~/Documents, `python .../hooks/xsm-hook.py` answered "Operation not permitted"
with status 2, and every prompt of every session was blocked. xsm exits 2 on
purpose nowhere."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_xsm  # noqa: E402

# Module names only: a class imported here by name would run its tests twice.
TempState = test_xsm.TempState
REPO = test_xsm.REPO
GUARD = "|| exit 1"


def missing(name):
    """A script path Python cannot open, as the person's terminal could not open
    one in ~/Documents ("Operation not permitted", Errno 1). A file that is not
    there gives the same status 2 ("No such file", Errno 2) to any user, root too."""
    return os.path.join(name, "Documents", "xsm", "hooks", "xsm-hook.py")


class CommandTest(TempState):
    def _entry(self):
        return os.path.join(REPO, "hooks", "xsm-hook.py")

    def _launcher(self):
        return '"%s"' % os.path.join(REPO, "hooks", "xsm-hook")

    def _state(self):
        """A test's state folder is not ~/.xsm, so the command names it."""
        return "XSM_HOME=%s " % self.tmp

    def test_the_claude_command_is_the_sh_launcher_before_the_marker(self):
        from xsm import install
        install.pin_python(sys.executable)
        for event in install.CLAUDE_EVENTS + ("PermissionRequest",):
            with self.subTest(event):
                self.assertEqual(install.hook_command("claude", event),
                                 "%s%s %s" % (self._state(), self._launcher(), install.MARKER))
        with mock.patch.dict(os.environ, {"XSM_HOME": "/elsewhere/xsm"}):
            self.assertEqual(install.hook_command("claude", "SessionStart"),
                             "XSM_HOME=/elsewhere/xsm %s %s" % (self._launcher(), install.MARKER))
        self.assertNotIn(GUARD, install.hook_command("claude", "SessionStart"),
                         "the launcher needs no guard: it turns status 2 into 1 itself")

    def test_the_codex_command_is_left_as_it_was(self):
        """A changed command makes Codex's hooks untrusted again on every home."""
        from xsm import install
        install.pin_python(sys.executable)
        self.assertEqual(install.hook_command("codex", "SessionStart"),
                         "%s%s %s %s" % (self._state(), sys.executable, self._entry(),
                                         install.MARKER))

    def test_a_script_python_cannot_open_is_status_2_bare_and_1_through_the_launcher(self):
        from xsm import install
        install.pin_python(sys.executable)
        script = missing(self.tmp)
        bare = subprocess.run([sys.executable, script], input="{}", capture_output=True, text=True)
        self.assertEqual(bare.returncode, 2, "the premise: Python's own status when it cannot open it")
        tree = os.path.join(self.tmp, "runtime-without-script")
        os.makedirs(os.path.join(tree, "hooks"))
        shutil.copy(os.path.join(REPO, "hooks", "xsm-hook"), os.path.join(tree, "hooks", "xsm-hook"))
        command = install.hook_command("claude", "UserPromptSubmit", root=tree)
        out = subprocess.run(["sh", "-c", command], input="{}", capture_output=True, text=True)
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("can't open file", out.stderr, "the reason still reaches the person")

    def test_a_script_that_runs_keeps_its_status_and_its_words(self):
        from xsm import install
        install.pin_python(sys.executable)
        ok = subprocess.run(["sh", "-c", install.hook_command("claude", "SessionEnd")],
                            input="{}", capture_output=True, text=True,
                            env=dict(os.environ, XSM_HOME=self.tmp))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        tree = os.path.join(self.tmp, "runtime-with-fake")
        os.makedirs(os.path.join(tree, "hooks"))
        shutil.copy(os.path.join(REPO, "hooks", "xsm-hook"), os.path.join(tree, "hooks", "xsm-hook"))
        with open(os.path.join(tree, "hooks", "xsm-hook.py"), "w") as fh:
            fh.write("import sys; sys.stdout.write('said'); sys.exit(0)\n")
        command = install.hook_command("claude", "SessionStart", root=tree)
        out = subprocess.run(["sh", "-c", command], capture_output=True, text=True,
                             env=dict(os.environ, XSM_HOME=self.tmp))
        self.assertEqual((out.returncode, out.stdout), (0, "said"))

    def test_a_pinned_python_that_went_away_does_not_stop_the_hook(self):
        """A brew upgrade removes the interpreter the pin names; the launcher
        goes on to the next one instead of failing the event."""
        from xsm import install
        install.pin_python("/opt/homebrew/Cellar/python@3.9/0.0.0/bin/python3")
        tree = os.path.join(self.tmp, "runtime-with-fake")
        os.makedirs(os.path.join(tree, "hooks"))
        shutil.copy(os.path.join(REPO, "hooks", "xsm-hook"), os.path.join(tree, "hooks", "xsm-hook"))
        with open(os.path.join(tree, "hooks", "xsm-hook.py"), "w") as fh:
            fh.write("import sys; sys.stdout.write('said'); sys.exit(0)\n")
        command = install.hook_command("claude", "SessionStart", root=tree)
        out = subprocess.run(["sh", "-c", command], capture_output=True, text=True,
                             env=dict(os.environ, XSM_HOME=self.tmp))
        self.assertEqual((out.returncode, out.stdout), (0, "said"), out.stderr)


class InstallTest(TempState):
    def _home(self):
        from xsm import paths
        home = os.path.join(self.tmp, "claude-guard")
        os.makedirs(home)
        paths.write_json(os.path.join(home, "settings.json"), {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "sh ~/mine.sh"}]}]}}, mode=0o644)
        return home

    def _old_form(self, home, guarded=False):
        """What 0.4.14 and earlier wrote (`python <script>`), or with `guarded`
        the form before the launcher (`python <script> || exit 1`)."""
        from xsm import install, paths
        target = os.path.join(home, "settings.json")
        data = paths.read_json(target)
        for event in install.CLAUDE_EVENTS:
            command = "XSM_HOME=%s %s %s%s %s" % (
                self.tmp, sys.executable, os.path.join(REPO, "hooks", "xsm-hook.py"),
                " " + GUARD if guarded else "", install.MARKER)
            group = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
            if event in install.MATCHERS:
                group["matcher"] = install.MATCHERS[event]
            data["hooks"].setdefault(event, []).append(group)
        paths.write_json(target, data, mode=0o644)

    def test_an_old_form_is_replaced_in_place_not_added_beside(self):
        from xsm import install, paths
        for guarded in (False, True):
            with self.subTest(guarded=guarded):
                home = self._home()
                self._old_form(home, guarded)
                plan = install.plan(home, "claude")
                self.assertEqual({a["event"]: a["action"] for a in plan["actions"]},
                                 {e: "replace" for e in install.CLAUDE_EVENTS})
                self.assertEqual({tuple(a["forms"]) for a in plan["actions"]},
                                 {("guarded",) if guarded else ("unguarded",)})
                install.apply(home, "claude")
                hooks = paths.read_json(os.path.join(home, "settings.json"))["hooks"]
                for event in install.CLAUDE_EVENTS:
                    ours = [g for g in hooks[event] if install._is_ours(g)]
                    self.assertEqual([g["hooks"][0]["command"] for g in ours],
                                     [install.hook_command("claude", event)], event)
                self.assertEqual(hooks["UserPromptSubmit"][0]["hooks"][0]["command"],
                                 "sh ~/mine.sh", "a hook that is not ours stays")
                self.assertTrue(install.apply(home, "claude").get("unchanged"),
                                "and then it is kept")
                shutil.rmtree(home)

    def test_a_home_already_on_the_new_form_is_kept(self):
        from xsm import install
        home = self._home()
        install.apply(home, "claude")
        plan = install.plan(home, "claude")
        self.assertEqual({a["action"] for a in plan["actions"]}, {"keep"})
        self.assertEqual({tuple(a["forms"]) for a in plan["actions"]}, {("launcher",)})

    def test_a_default_home_prefix_on_the_old_form_is_still_upgraded(self):
        from xsm import install, paths
        home = self._home()
        self._old_form(home)
        target = os.path.join(home, "settings.json")
        data = paths.read_json(target)
        for groups in data["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    if install.MARKER in hook["command"]:
                        hook["command"] = hook["command"].replace(
                            "XSM_HOME=%s" % self.tmp, "XSM_HOME=%s" % os.path.expanduser("~/.xsm"))
        paths.write_json(target, data, mode=0o644)
        self.assertEqual({a["action"] for a in install.plan(home, "claude")["actions"]},
                         {"replace"})

    def test_uninstall_takes_out_every_form(self):
        from xsm import install, paths
        home = self._home()
        self._old_form(home)
        self._old_form(home, guarded=True)
        install.apply(home, "claude")           # replaces both: one launcher group per event
        self._old_form(home)                    # one of each, as a half-done upgrade leaves them
        self._old_form(home, guarded=True)
        self.assertTrue(any(install.MARKER in json.dumps(g) for g in
                            paths.read_json(os.path.join(home, "settings.json"))["hooks"]["SessionEnd"]))
        install.remove(home, "claude")
        with open(os.path.join(home, "settings.json")) as fh:
            text = fh.read()
        self.assertNotIn(install.MARKER, text)
        self.assertIn("sh ~/mine.sh", text)

    def test_a_background_workers_hook_is_the_launcher_too(self):
        from xsm import workers
        self.assertIn(os.path.join("hooks", "xsm-hook") + '"', workers._hook_command())
        self.assertNotIn("xsm-hook.py", workers._hook_command())

    def test_doctor_names_the_old_forms_and_what_the_agent_runs(self):
        """The unguarded form can block every prompt, and doctor says so plainly;
        what fixes it is a command the agent runs, never one the person types."""
        from xsm import cli, config, install
        home = self._home()
        config.add_home(home, "claude")
        for guarded, needle in ((False, "BLOCKS every prompt"), (True, "no longer blocks")):
            with self.subTest(guarded=guarded):
                self._old_form(home, guarded)
                install.apply(home, "claude")
                self._old_form(home, guarded)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cli.main(["doctor"])
                lines = [l for l in out.getvalue().splitlines() if l.startswith("hooks ")]
                self.assertTrue(any(needle in l and "xsm install --refresh" in l for l in lines),
                                out.getvalue())
                install.remove(home, "claude")

    def test_doctor_names_a_hook_script_that_is_gone(self):
        from xsm import cli, config, install, paths
        home = self._home()
        config.add_home(home, "claude")
        target = os.path.join(home, "settings.json")
        data = paths.read_json(target)
        data["hooks"]["SessionStart"] = [{"hooks": [{
            "type": "command", "timeout": 10,
            "command": '"/nowhere/runtime/hooks/xsm-hook" %s' % install.MARKER}]}]
        paths.write_json(target, data, mode=0o644)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["doctor"])
        self.assertTrue(any("/nowhere/runtime/hooks/xsm-hook is gone" in l
                            for l in out.getvalue().splitlines()), out.getvalue())


class LauncherTest(TempState):
    """hooks/xsm-hook, which a plugin runs: Claude's and Codex's."""

    def _run(self, status, stdin="in"):
        """The launcher in a copy of the plugin folder whose xsm-hook.py ends
        with `status`, or is not there (None): Python cannot open it."""
        plugin = os.path.join(self.tmp, "plugin-%s" % status)
        os.makedirs(os.path.join(plugin, "hooks"))
        shutil.copy(os.path.join(REPO, "hooks", "xsm-hook"), os.path.join(plugin, "hooks", "xsm-hook"))
        if status is not None:
            with open(os.path.join(plugin, "hooks", "xsm-hook.py"), "w") as fh:
                fh.write("import sys\nsys.stdout.write(sys.stdin.read())\n"
                         "sys.stderr.write('warned')\nsys.exit(%d)\n" % status)
        env = dict(os.environ, XSM_PYTHON_CANDIDATES=sys.executable, XSM_HOME=self.tmp)
        return subprocess.run([os.path.join(plugin, "hooks", "xsm-hook")], input=stdin,
                              capture_output=True, text=True, env=env)

    def test_a_script_that_cannot_be_opened_is_status_1(self):
        out = self._run(None)
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("can't open file", out.stderr)

    def test_status_2_never_leaves_the_launcher(self):
        out = self._run(2)
        self.assertEqual(out.returncode, 1)

    def test_other_statuses_and_the_words_pass_through(self):
        for status in (0, 1, 3):
            with self.subTest(status):
                out = self._run(status, "in %d" % status)
                self.assertEqual((out.returncode, out.stdout, out.stderr),
                                 (status, "in %d" % status, "warned"))


class HookScriptTest(TempState):
    """hooks/xsm-hook.py: whatever the package does, never status 2."""

    def _run(self, receive):
        tree = tempfile.mkdtemp(dir=self.tmp)       # a new one each time: no stale bytecode
        os.makedirs(os.path.join(tree, "hooks"))
        os.makedirs(os.path.join(tree, "xsm"))
        shutil.copy(os.path.join(REPO, "hooks", "xsm-hook.py"), os.path.join(tree, "hooks"))
        open(os.path.join(tree, "xsm", "__init__.py"), "w").close()
        with open(os.path.join(tree, "xsm", "receive.py"), "w") as fh:
            fh.write(receive)
        return subprocess.run([sys.executable, os.path.join(tree, "hooks", "xsm-hook.py")],
                              input="{}", capture_output=True, text=True).returncode

    def test_a_main_that_answers_2_exits_1(self):
        self.assertEqual(self._run("def main():\n    return 2\n"), 1)

    def test_the_other_answers_stand(self):
        self.assertEqual(self._run("def main():\n    return 0\n"), 0)
        self.assertEqual(self._run("def main():\n    return 1\n"), 1)

    def test_a_package_that_does_not_import_is_not_status_2(self):
        self.assertNotEqual(self._run("raise ImportError('broken')\n"), 2)
        self.assertNotEqual(self._run("def main(:\n"), 2, "a syntax error")

    def _real(self, event, prompt="hi", **extra):
        """The real hook, made to fail inside (XSM_FORCE_ERROR): (status, stdout)."""
        data = dict({"hook_event_name": event, "prompt": prompt, "session_id": "s"}, **extra)
        out = subprocess.run([sys.executable, os.path.join(REPO, "hooks", "xsm-hook.py")],
                             input=json.dumps(data), capture_output=True, text=True,
                             env=dict(os.environ, XSM_HOME=self.tmp, XSM_FORCE_ERROR="1"))
        return out.returncode, out.stdout

    def test_an_internal_error_never_blocks_a_persons_own_prompt_or_any_other_event(self):
        for event, prompt in (("UserPromptSubmit", "응, 진행해"), ("UserPromptSubmit", ""),
                              ("UserPromptExpansion", "/xsm list"), ("SessionStart", ""),
                              ("SessionEnd", ""), ("PostToolUse", ""),
                              ("PermissionRequest", ""), ("Anything", "")):
            with self.subTest(event, prompt=prompt):
                self.assertEqual(self._real(event, prompt), (0, ""),
                                 "status 0 and no decision of any kind")

    def test_only_a_prompt_carrying_the_peer_envelope_is_held_when_the_gate_breaks(self):
        """The one decision a failure can still emit (S8-g2: a gate that breaks
        must not wave a peer message through). It never reaches a person's own
        text, and the status is 0, not the 2 that blocks a session."""
        status, out = self._real("UserPromptSubmit",
                                 '<cross-session-message from-mode="bypass">\nhi\n'
                                 '</cross-session-message>')
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertEqual(self._real("PostToolUse", "<cross-session-message>"), (0, ""),
                         "a tool result has happened: nothing to hold back")


class NoPythonTest(TempState):
    """The plugin launcher when no usable python is found."""

    def _run(self, stdin):
        plugin = os.path.join(self.tmp, "plugin-bare")
        os.makedirs(os.path.join(plugin, "hooks"), exist_ok=True)
        shutil.copy(os.path.join(REPO, "hooks", "xsm-hook"), os.path.join(plugin, "hooks", "xsm-hook"))
        out = subprocess.run([os.path.join(plugin, "hooks", "xsm-hook")], input=stdin,
                             capture_output=True, text=True,
                             env=dict(os.environ, XSM_PYTHON_CANDIDATES="/nonexistent/python",
                                      XSM_HOME=self.tmp))
        return out.returncode, out.stdout

    def test_a_persons_prompt_passes_and_only_a_peer_envelope_is_held(self):
        for event, prompt in (("UserPromptSubmit", "응, 진행해"), ("SessionStart", ""),
                              ("PermissionRequest", ""), ("PostToolUse", "")):
            with self.subTest(event):
                self.assertEqual(self._run(json.dumps({"hook_event_name": event,
                                                       "prompt": prompt})), (0, ""))
        status, out = self._run(json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": "<cross-session-message>\nhi\n</cross-session-message>"}))
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)["decision"], "block")


if __name__ == "__main__":
    unittest.main()
