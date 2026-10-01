"""`xsm install` runs the hooks, the MCP server and `xsm` from a copy of the
checkout under ~/.xsm/runtime, not from the checkout (user decision, 2026-10-01).

macOS can deny a session's app the folder a checkout lives in (~/Documents), and
`python .../Documents/.../xsm-hook.py` then answered status 2, which Claude Code
reads as "block": every prompt of every session stopped. The hook is a launcher
that cannot block, and each default here is open and can be switched (policy.py)."""
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


class _Checkout(TempState):
    """A copy of this repository to change, standing in for the checkout."""

    def setUp(self):
        super().setUp()
        from xsm import install
        self.checkout = os.path.join(self.tmp, "checkout")
        for part in ("xsm", "hooks", "bin", "skills"):
            shutil.copytree(os.path.join(REPO, part), os.path.join(self.checkout, part),
                            ignore=shutil.ignore_patterns("__pycache__"))
        for manifest in (".claude-plugin", ".codex-plugin"):
            shutil.copytree(os.path.join(REPO, manifest), os.path.join(self.checkout, manifest))
        patch = mock.patch.object(install, "REPO", self.checkout)
        patch.start()
        self.addCleanup(patch.stop)

    def _edit(self, text="# changed\n"):
        with open(os.path.join(self.checkout, "xsm", "paths.py"), "a") as fh:
            fh.write(text)


class PolicyTest(TempState):
    def test_every_default_is_the_open_one(self):
        from xsm import policy
        self.assertEqual({n: policy.get(n) for n in policy.POLICIES},
                         {"runtime": "snapshot", "claude_inbound": "accept",
                          "allow_messaging": True, "human_send_connects": True})

    def test_config_json_switches_one_and_the_environment_wins(self):
        from xsm import config, paths, policy
        paths.write_json(paths.path(config.CONFIG), {"runtime": "checkout",
                                                    "allow_messaging": False})
        self.assertEqual((policy.get("runtime"), policy.get("allow_messaging")),
                         ("checkout", False))
        with mock.patch.dict(os.environ, {"XSM_RUNTIME": "snapshot",
                                          "XSM_ALLOW_MESSAGING": "yes"}):
            self.assertEqual((policy.get("runtime"), policy.get("allow_messaging")),
                             ("snapshot", True), "XSM_<NAME> wins over the file")
        with mock.patch.dict(os.environ, {"XSM_CLAUDE_INBOUND": "leave",
                                          "XSM_HUMAN_SEND_CONNECTS": "off"}):
            self.assertEqual((policy.get("claude_inbound"), policy.get("human_send_connects")),
                             ("leave", False))

    def test_a_value_the_setting_does_not_take_never_closes_anything(self):
        from xsm import config, paths, policy
        paths.write_json(paths.path(config.CONFIG), {
            "runtime": "somewhere", "claude_inbound": ["hold"], "allow_messaging": "banana",
            "human_send_connects": [0]})
        with mock.patch.dict(os.environ, {"XSM_RUNTIME": "nonsense",
                                          "XSM_ALLOW_MESSAGING": "maybe"}):
            self.assertEqual({n: policy.get(n) for n in policy.POLICIES},
                             {n: policy.default(n) for n in policy.POLICIES})

    def test_a_bad_environment_value_falls_through_to_the_file_not_to_the_default(self):
        """2026-10-02: policy.get did, config.policy did not; there is one reader now."""
        from xsm import config, paths, policy
        paths.write_json(paths.path(config.CONFIG), {
            "runtime": "checkout", "allow_messaging": False, "fail_open": False,
            "remote_native": "hold"})
        with mock.patch.dict(os.environ, {"XSM_RUNTIME": "nonsense", "XSM_ALLOW_MESSAGING": "maybe",
                                          "XSM_FAIL_OPEN": "maybe", "XSM_REMOTE_NATIVE": "later"}):
            self.assertEqual((policy.get("runtime"), policy.get("allow_messaging")),
                             ("checkout", False))
            self.assertEqual((config.policy("fail_open"), config.policy("remote_native")),
                             (False, "hold"))
        with mock.patch.object(config, "policy", return_value="x") as reader:
            self.assertEqual(policy.get("runtime"), "x", "one implementation: policy.get asks it")
        reader.assert_called_once()

    def test_the_two_entry_points_read_the_same_file_the_same_way(self):
        from xsm import config, paths, policy
        for raw, want in (("no", False), ("OFF", False), (0, False), (" false ", False),
                          ("yes", True), (1, True), (True, True), ("banana", True)):
            with self.subTest(raw=raw):
                paths.write_json(paths.path(config.CONFIG), {"allow_messaging": raw})
                self.assertIs(policy.get("allow_messaging"), want)
                self.assertIs(config.policy("allow_messaging", True), want)

    def test_doctor_names_what_is_not_at_its_default_on_one_line(self):
        from xsm import config, paths
        paths.write_json(paths.path(config.CONFIG), {"claude_inbound": "leave"})
        with mock.patch.dict(os.environ, {"XSM_ALLOW_MESSAGING": "0"}):
            code, text = _run_cli("doctor")
        lines = [l for l in text.splitlines() if l.startswith("policy ")]
        self.assertEqual(len(lines), 1, text)
        self.assertIn("allow_messaging=false", lines[0])
        self.assertIn("claude_inbound=leave", lines[0])
        self.assertTrue(lines[0].endswith("(not the default: allow_messaging, claude_inbound)"),
                        lines[0])
        code, text = _run_cli("doctor")
        line = [l for l in text.splitlines() if l.startswith("policy ")][0]
        self.assertTrue(line.endswith("(not the default: claude_inbound)"), line)
        paths.write_json(paths.path(config.CONFIG), {})
        line = [l for l in _run_cli("doctor")[1].splitlines() if l.startswith("policy ")][0]
        self.assertNotIn("not the default", line)


class SnapshotTest(_Checkout):
    def test_the_snapshot_is_what_xsm_needs_at_run_time_and_runs_outside_the_checkout(self):
        from xsm import install
        snap = install.make_snapshot()
        root = snap["path"]
        self.assertEqual(os.path.dirname(root), os.path.join(self.tmp, "runtime"))
        self.assertEqual(snap["id"], os.path.basename(root))
        self.assertRegex(snap["id"], r"\A[0-9a-f]{12}\Z")
        for rel in ("xsm/cli.py", "xsm/install.py", "hooks/xsm-hook", "hooks/xsm-hook.py",
                    "hooks/xsm-mcp", "hooks/xsm-mcp.py", "hooks/xsm-remote.py", "hooks/hooks.json",
                    "hooks/codex-hooks.json", "skills/xsm/SKILL.md",
                    "skills/xsm/references/guide.md", "bin/xsm", ".claude-plugin/plugin.json",
                    ".codex-plugin/plugin.json"):
            self.assertTrue(os.path.isfile(os.path.join(root, rel)), rel)
        for rel in ("hooks/xsm-hook", "hooks/xsm-mcp", "bin/xsm"):
            self.assertTrue(os.access(os.path.join(root, rel), os.X_OK), rel)
        self.assertFalse(os.path.exists(os.path.join(root, "tests")))
        self.assertEqual(install.runtime_root(), root)
        self.assertEqual(install.launcher(), os.path.join(root, "bin", "xsm"))
        self.assertEqual(install.mcp_command(), [os.path.join(root, "hooks", "xsm-mcp")])
        self.assertEqual(os.path.realpath(os.path.join(self.tmp, "runtime", "current")),
                         os.path.realpath(root))
        out = subprocess.run([os.path.join(root, "bin", "xsm"), "--version"], capture_output=True,
                             text=True, env=dict(os.environ, XSM_HOME=self.tmp), cwd=self.tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(root, out.stdout, "it runs from the copy, not from the checkout")

    def test_the_same_code_is_the_same_folder_and_changed_code_is_a_new_one(self):
        from xsm import install
        first = install.make_snapshot()
        self.assertEqual(install.make_snapshot()["id"], first["id"])
        self._edit()
        second = install.make_snapshot()
        self.assertNotEqual(second["id"], first["id"])
        self.assertTrue(os.path.isdir(first["path"]), "the older one stays until nothing uses it")
        self.assertEqual(install.runtime_current()["id"], second["id"])
        self.assertTrue(os.path.exists(os.path.join(first["path"], install.RETIRED)))

    def test_an_edit_inside_the_snapshot_is_put_right_not_trusted(self):
        from xsm import install
        snap = install.make_snapshot()
        with open(os.path.join(snap["path"], "xsm", "paths.py"), "a") as fh:
            fh.write("# someone's edit\n")
        install.make_snapshot()
        with open(os.path.join(snap["path"], "xsm", "paths.py")) as fh, \
                open(os.path.join(self.checkout, "xsm", "paths.py")) as want:
            self.assertEqual(fh.read(), want.read())

    def test_dev_and_the_policy_keep_the_checkout_and_a_snapshot_is_not_copied_again(self):
        from xsm import install
        with mock.patch.dict(os.environ, {"XSM_RUNTIME": "checkout"}):
            self.assertEqual(install.runtime_root(), self.checkout)
            self.assertEqual(install.launcher(), os.path.join(self.checkout, "bin", "xsm"))
        snap = install.make_snapshot()
        with mock.patch.object(install, "REPO", snap["path"]):
            self.assertTrue(install.runtime_in_place())
            self.assertEqual(install.runtime_root(), snap["path"])

    def test_a_plugin_copy_is_a_runtime_already(self):
        from xsm import install
        plugin = os.path.join(self.tmp, "home", "plugins", "cache", "xsm", "xsm", "0.4.15")
        os.makedirs(plugin)
        with mock.patch.object(install, "REPO", plugin):
            self.assertTrue(install.runtime_in_place())
            self.assertEqual(install.runtime_root(), plugin)

    def test_the_hook_mcp_and_path_all_name_the_snapshot_after_an_install(self):
        from xsm import install, paths
        home = os.path.join(self.tmp, "claude-snap")
        code, text = _run_cli("install", "--claude-home", home, "--no-mcp", "--python",
                              sys.executable)
        self.assertEqual(code, 0, text)
        snap = install.runtime_current()
        self.assertIn("runtime: %s" % snap["path"].replace(self.user_home, "~"), text)
        data = paths.read_json(os.path.join(home, "settings.json"))
        for event in install.CLAUDE_EVENTS:
            command = data["hooks"][event][0]["hooks"][0]["command"]
            self.assertIn('"%s"' % os.path.join(snap["path"], "hooks", "xsm-hook"), command, event)
            self.assertNotIn(self.checkout, command)
            self.assertNotIn("xsm-hook.py", command)
        link = os.path.join(self.user_home, ".local", "bin", "xsm")
        self.assertEqual(os.path.realpath(link), os.path.realpath(
            os.path.join(snap["path"], "bin", "xsm")))
        self.assertIn("cli: linked", text)

    def test_a_dry_run_copies_nothing_and_says_where_it_would_run_from(self):
        from xsm import install
        home = os.path.join(self.tmp, "claude-dry")
        code, text = _run_cli("install", "--claude-home", home, "--dry-run", "--python",
                              sys.executable)
        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "runtime")))
        self.assertFalse(os.path.exists(os.path.join(home, "settings.json")))
        planned = install.runtime_root(planned=True)
        self.assertIn(planned.replace(self.user_home, "~"), text)
        self.assertIn(os.path.join(planned, "hooks", "xsm-hook"), text)

    def test_dev_points_the_hooks_at_the_checkout_and_a_refresh_keeps_them_there(self):
        """2026-10-02: --dev is saved as the policy, so a refresh or doctor run without the
        flag neither moves the hooks back to a copy nor calls the dev install out of date."""
        from xsm import config, install, paths
        home = os.path.join(self.tmp, "claude-dev")
        target = os.path.join(home, "settings.json")
        code, text = _run_cli("install", "--claude-home", home, "--no-mcp", "--dev",
                              "--python", sys.executable)
        self.assertEqual(code, 0, text)
        self.assertIn("this checkout", text)
        self.assertIn("runtime=checkout is saved in", text)
        self.assertEqual(config.load()["runtime"], "checkout")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "runtime", "current.json")))
        command = paths.read_json(target)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn(os.path.join(self.checkout, "hooks", "xsm-hook"), command)
        link = os.path.join(self.user_home, ".local", "bin", "xsm")
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(self.checkout, "bin", "xsm")))
        code, text = _run_cli("install", "--refresh", "--no-mcp", "--python", sys.executable)
        self.assertEqual(code, 0, text)
        self.assertIsNone(install.runtime_current(), "no copy was made behind the person's back")
        command = paths.read_json(target)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn(os.path.join(self.checkout, "hooks", "xsm-hook"), command)
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(self.checkout, "bin", "xsm")))
        actions = [a["action"] for a in install.plan(home, "claude")["actions"]]
        self.assertEqual(set(actions), {"keep"}, "doctor sees nothing to replace")
        # And a copy installed before is not what doctor measures it against.
        install.make_snapshot()
        self.assertEqual({a["action"] for a in install.plan(home, "claude")["actions"]}, {"keep"})
        code, text = _run_cli("doctor")
        self.assertIn("the checkout", [l for l in text.splitlines() if l.startswith("runtime ")][0])
        self.assertNotIn(":replace", text)

    def test_going_back_to_a_copy_is_deleting_the_line(self):
        from xsm import config, install, paths
        home = os.path.join(self.tmp, "claude-back")
        target = os.path.join(home, "settings.json")
        _run_cli("install", "--claude-home", home, "--no-mcp", "--dev", "--python", sys.executable)
        raw = config.load()
        raw.pop("runtime")
        paths.write_json(paths.path(config.CONFIG), raw)
        code, text = _run_cli("install", "--refresh", "--no-mcp", "--python", sys.executable)
        self.assertEqual(code, 0, text)
        snap = install.runtime_current()
        command = paths.read_json(target)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn(os.path.join(snap["path"], "hooks", "xsm-hook"), command)

    def test_dev_says_nothing_new_the_second_time_and_a_dry_run_saves_nothing(self):
        from xsm import config
        home = os.path.join(self.tmp, "claude-dev2")
        code, text = _run_cli("install", "--claude-home", home, "--no-mcp", "--dev", "--dry-run",
                              "--python", sys.executable)
        self.assertEqual(code, 0, text)
        self.assertNotIn("runtime", config.load())
        _run_cli("install", "--claude-home", home, "--no-mcp", "--dev", "--python", sys.executable)
        code, text = _run_cli("install", "--claude-home", home, "--no-mcp", "--dev",
                              "--python", sys.executable)
        self.assertNotIn("is saved in", text)

    def test_a_second_refresh_with_nothing_changed_rewrites_nothing(self):
        """2026-10-02: current.json (its `made`) and the `current` link were rewritten by
        every refresh, so a refresh that changed nothing still changed the files."""
        from xsm import install
        first = install.make_snapshot()
        record = os.path.join(self.tmp, "runtime", "current.json")
        link = os.path.join(self.tmp, "runtime", "current")
        before = (os.stat(record).st_mtime_ns, os.lstat(link).st_ino, os.lstat(link).st_mtime_ns)
        time.sleep(0.02)
        second = install.make_snapshot()
        after = (os.stat(record).st_mtime_ns, os.lstat(link).st_ino, os.lstat(link).st_mtime_ns)
        self.assertEqual(before, after)
        self.assertEqual((second["id"], second["path"], second["made"]),
                         (first["id"], first["path"], first["made"]))
        # A change, a missing link, or a mark of retirement is still put right.
        os.unlink(link)
        install.make_snapshot()
        self.assertEqual(os.readlink(link), first["id"])
        self._edit()
        third = install.make_snapshot()
        self.assertNotEqual(third["id"], first["id"])

    def test_uninstall_unlinks_the_path_link_into_a_copy_and_says_the_folder_can_go(self):
        from xsm import install
        home = os.path.join(self.tmp, "claude-un")
        _run_cli("install", "--claude-home", home, "--no-mcp", "--python", sys.executable)
        link = os.path.join(self.user_home, ".local", "bin", "xsm")
        self.assertTrue(os.path.islink(link))
        code, text = _run_cli("uninstall", "--claude-home", home)
        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.lexists(link), "a link into the copy is not left dangling")
        self.assertIn("removed the ~/.local/bin/xsm link", text)
        self.assertIn("delete that folder", text)
        self.assertTrue(os.path.isdir(install.runtime_dir()), "the copies are the person's to delete")

    def test_uninstall_of_one_home_leaves_the_link_while_another_keeps_the_hooks(self):
        a, b = (os.path.join(self.tmp, "claude-" + n) for n in ("a", "b"))
        for home in (a, b):
            _run_cli("install", "--claude-home", home, "--no-mcp", "--python", sys.executable)
        link = os.path.join(self.user_home, ".local", "bin", "xsm")
        _run_cli("uninstall", "--claude-home", a)
        self.assertTrue(os.path.islink(link))
        _run_cli("uninstall", "--claude-home", b)
        self.assertFalse(os.path.lexists(link))

    def test_uninstall_leaves_a_link_to_a_checkout_alone(self):
        home = os.path.join(self.tmp, "claude-dev-un")
        _run_cli("install", "--claude-home", home, "--no-mcp", "--dev", "--python", sys.executable)
        link = os.path.join(self.user_home, ".local", "bin", "xsm")
        _run_cli("uninstall", "--claude-home", home)
        self.assertTrue(os.path.islink(link), "it points at the person's checkout")

    def test_a_refresh_after_a_change_replaces_the_hooks_in_place_and_the_link(self):
        from xsm import install, paths
        home = os.path.join(self.tmp, "claude-refresh")
        target = os.path.join(home, "settings.json")
        _run_cli("install", "--claude-home", home, "--no-mcp", "--python", sys.executable)
        first = install.runtime_current()
        self._edit()
        with mock.patch.object(install, "_running_from", return_value=set(first["id"])):
            code, text = _run_cli("install", "--refresh", "--no-mcp", "--python", sys.executable)
        self.assertEqual(code, 0, text)
        second = install.runtime_current()
        self.assertNotEqual(second["id"], first["id"])
        groups = paths.read_json(target)["hooks"]["SessionStart"]
        self.assertEqual(len(groups), 1, "replaced, not added beside")
        self.assertIn(second["path"], groups[0]["hooks"][0]["command"])
        link = os.path.join(self.user_home, ".local", "bin", "xsm")
        self.assertEqual(os.path.realpath(link), os.path.realpath(
            os.path.join(second["path"], "bin", "xsm")))

    def test_the_skill_link_follows_the_runtime_too(self):
        """A session macOS keeps out of the checkout's folder cannot read a skill
        that lives there either; a link into the checkout, or into an earlier
        copy, is stale and a refresh moves it."""
        from xsm import install
        home = os.path.join(self.tmp, "claude-skill")
        link = os.path.join(home, "skills", "xsm")
        _run_cli("install", "--claude-home", home, "--no-mcp", "--python", sys.executable)
        first = install.runtime_current()
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(first["path"], "skills", "xsm")))
        self.assertEqual(install.skill_state(home)[0], "linked")
        self._edit()
        with mock.patch.object(install, "_running_from", return_value={first["id"]}):
            install.make_snapshot()
            self.assertEqual(install.skill_state(home)[0], "link-stale")
            _run_cli("install", "--refresh", "--no-mcp", "--python", sys.executable)
        second = install.runtime_current()
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(second["path"], "skills", "xsm")))
        # An earlier install's link to the checkout itself is moved the same way.
        os.unlink(link)
        os.symlink(os.path.join(self.checkout, "skills", "xsm"), link)
        self.assertEqual(install.skill_state(home)[0], "link-stale")
        with mock.patch.object(install, "_running_from", return_value=set()):
            _run_cli("install", "--refresh", "--no-mcp", "--python", sys.executable)
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(second["path"], "skills", "xsm")))
        with mock.patch.dict(os.environ, {"XSM_RUNTIME": "checkout"}):
            self.assertEqual(install.skill_state(home)[0], "link-stale", "--dev moves it back")
        self.assertTrue(install.remove_skill(home))
        self.assertFalse(os.path.lexists(link))

    def test_the_mcp_server_is_registered_with_the_launcher_not_a_python_path(self):
        from xsm import install
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            if argv[1:4] == ["mcp", "get", "xsm"]:
                return subprocess.CompletedProcess(argv, 1, "", "No MCP server found")
            return subprocess.CompletedProcess(argv, 0, "", "")

        snap = install.make_snapshot()
        home = os.path.join(self.tmp, "claude-mcp")
        os.makedirs(home)
        with mock.patch.object(install.subprocess, "run", fake):
            self.assertEqual(install.install_mcp(home, "claude"), "added")
        add = next(c for c in calls if c[1:3] == ["mcp", "add"])
        self.assertEqual(add[-1], os.path.join(snap["path"], "hooks", "xsm-mcp"))
        self.assertEqual(add[add.index("--") + 1:], [os.path.join(snap["path"], "hooks", "xsm-mcp")])
        self.assertNotIn(sys.executable, add)
        self.assertNotIn("xsm-mcp.py", " ".join(add))

    def test_a_codex_direct_install_keeps_its_command_and_a_new_one_gets_a_stable_path(self):
        """The hook text is part of Codex's trust hash: a refresh must not change
        it, so a new install names ~/.xsm/runtime/current, which the snapshots
        swap behind (hooks/codex-hooks.json is not touched)."""
        from xsm import install, paths
        home = os.path.join(self.tmp, "codex-direct")
        target = os.path.join(home, "hooks.json")
        install.pin_python(sys.executable)
        _run_cli("install", "--codex-home", home, "--no-mcp", "--python", sys.executable)
        first = paths.read_json(target)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        stable = os.path.join(self.tmp, "runtime", "current", "hooks", "xsm-hook.py")
        self.assertIn(stable, first)
        self._edit()
        with mock.patch.object(install, "_running_from", return_value=set()):
            _run_cli("install", "--refresh", "--no-mcp", "--python", sys.executable)
        self.assertEqual(paths.read_json(target)["hooks"]["SessionStart"][0]["hooks"][0]["command"],
                         first, "the same text, so Codex asks nothing again")
        # An install made before snapshots keeps its script wherever it is.
        old = os.path.join(self.tmp, "old-checkout", "hooks", "xsm-hook.py")
        os.makedirs(os.path.dirname(old))
        open(old, "w").close()
        existing = "%s %s %s" % (sys.executable, old, install.MARKER)
        self.assertIn(old, install.hook_command("codex", "SessionStart", existing=existing))

    def test_the_codex_plugin_file_is_not_touched(self):
        """Its text is what the plugin's trust hash is made of."""
        with open(os.path.join(REPO, "hooks", "codex-hooks.json")) as fh:
            for group in json.load(fh)["hooks"].values():
                self.assertEqual(group[0]["hooks"][0]["command"],
                                 '"${PLUGIN_ROOT}/hooks/xsm-hook"')


class OtherCheckoutTest(_Checkout):
    """2026-10-02: installing from a different checkout (a worktree, a clone) left
    `~/.local/bin/xsm` and `<home>/skills/xsm` "foreign" when they pointed at another
    xsm checkout, so the hooks and the MCP server moved to the new copy while the CLI
    and the skill stayed on the old code, and the skill was reported as "something
    else". A link into any xsm checkout or into the runtime is ours; a real foreign
    file is still left alone."""

    def _other(self, name="other-xsm", manifest=None, init=True):
        """Another xsm checkout: what makes it one is `xsm/__init__.py` and a plugin
        manifest naming xsm; the launcher and the skill are enough for the rest."""
        root = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(root, "xsm"))
        os.makedirs(os.path.join(root, "bin"))
        os.makedirs(os.path.join(root, ".claude-plugin"))
        os.makedirs(os.path.join(root, "skills", "xsm"))
        if init:
            open(os.path.join(root, "xsm", "__init__.py"), "w").close()
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w") as fh:
            json.dump(manifest or {"name": "xsm"}, fh)
        with open(os.path.join(root, "bin", "xsm"), "w") as fh:
            fh.write("#!/bin/sh\n")
        with open(os.path.join(root, "skills", "xsm", "SKILL.md"), "w") as fh:
            fh.write("---\nname: xsm\n---\n")
        return root

    def _cli_link(self):
        return os.path.join(self.user_home, ".local", "bin", "xsm")

    def test_a_cli_link_into_another_xsm_checkout_moves_to_the_runtime(self):
        from xsm import install
        other = self._other()
        os.makedirs(os.path.dirname(self._cli_link()))
        os.symlink(os.path.join(other, "bin", "xsm"), self._cli_link())
        self.assertEqual(install.install_cli(), "replaced")
        self.assertEqual(os.path.realpath(self._cli_link()), os.path.realpath(install.launcher()))
        self.assertEqual(install.install_cli(), "current")

    def test_and_so_does_one_into_the_runtime_of_an_earlier_install(self):
        from xsm import install
        first = install.make_snapshot()
        os.makedirs(os.path.dirname(self._cli_link()))
        os.symlink(os.path.join(first["path"], "bin", "xsm"), self._cli_link())
        self._edit()
        install.make_snapshot()
        self.assertEqual(install.install_cli(), "replaced")

    def test_a_link_to_something_that_is_not_an_xsm_checkout_is_still_foreign(self):
        from xsm import install
        os.makedirs(os.path.dirname(self._cli_link()))
        for name, kw in (("no-manifest-name", {"manifest": {"name": "another-tool"}}),
                         ("no-package", {"init": False})):
            with self.subTest(name):
                other = self._other(name, **kw)
                if os.path.lexists(self._cli_link()):
                    os.unlink(self._cli_link())
                os.symlink(os.path.join(other, "bin", "xsm"), self._cli_link())
                self.assertEqual(install.install_cli(), "foreign")
                self.assertEqual(os.readlink(self._cli_link()), os.path.join(other, "bin", "xsm"))

    def test_a_plugin_version_copy_is_not_a_checkout_and_a_plugin_run_takes_nothing(self):
        from xsm import install
        plugin = self._other(os.path.join("plugins", "cache", "xsm", "xsm", "0.4.15"))
        self.assertFalse(install._xsm_checkout(os.path.join(plugin, "bin", "xsm"), 2, "bin", "xsm"))
        other = self._other()
        os.makedirs(os.path.dirname(self._cli_link()))
        os.symlink(os.path.join(other, "bin", "xsm"), self._cli_link())
        with mock.patch.object(install, "REPO", plugin):
            self.assertEqual(install.install_cli(), "foreign", "a plugin copy never takes a link")
        self.assertEqual(os.readlink(self._cli_link()), os.path.join(other, "bin", "xsm"))

    def test_a_skill_link_into_another_xsm_checkout_is_stale_not_something_else(self):
        from xsm import install
        other = self._other()
        home = os.path.join(self.tmp, "claude-skill-other")
        link = os.path.join(home, "skills", "xsm")
        os.makedirs(os.path.dirname(link))
        os.symlink(os.path.join(other, "skills", "xsm"), link)
        self.assertEqual(install.skill_state(home)[0], "link-stale")
        self.assertEqual(install.install_skill(home)[0], "link-stale", "plain install says so")
        self.assertEqual(install.install_skill(home, refresh=True)[0], "linked")
        self.assertEqual(os.path.realpath(link), os.path.realpath(install.skill_source()))

    def test_a_skill_link_to_a_folder_that_is_no_xsm_checkout_is_left_alone(self):
        from xsm import install
        other = self._other("not-xsm", manifest={"name": "another-tool"})
        home = os.path.join(self.tmp, "claude-skill-foreign")
        link = os.path.join(home, "skills", "xsm")
        os.makedirs(os.path.dirname(link))
        os.symlink(os.path.join(other, "skills", "xsm"), link)
        self.assertEqual(install.skill_state(home)[0], "foreign")
        self.assertEqual(install.install_skill(home, refresh=True)[0], "foreign")
        self.assertEqual(os.readlink(link), os.path.join(other, "skills", "xsm"))

    def test_uninstall_takes_the_skill_link_an_install_from_another_checkout_made(self):
        from xsm import install
        other = self._other()
        home = os.path.join(self.tmp, "claude-skill-un")
        link = os.path.join(home, "skills", "xsm")
        os.makedirs(os.path.dirname(link))
        os.symlink(os.path.join(other, "skills", "xsm"), link)
        self.assertTrue(install.remove_skill(home))
        self.assertFalse(os.path.lexists(link))

    def test_the_whole_install_from_the_second_checkout_leaves_nothing_on_the_old_code(self):
        """The reported mix, end to end: the CLI link and the skill link, with --refresh."""
        other = self._other()
        home = os.path.join(self.tmp, "claude-mix")
        os.makedirs(os.path.join(home, "skills"))
        os.makedirs(os.path.dirname(self._cli_link()))
        os.symlink(os.path.join(other, "bin", "xsm"), self._cli_link())
        os.symlink(os.path.join(other, "skills", "xsm"), os.path.join(home, "skills", "xsm"))
        code, text = _run_cli("install", "--claude-home", home, "--no-mcp", "--refresh",
                              "--python", sys.executable)
        self.assertEqual(code, 0, text)
        self.assertNotIn("is something else", text)
        self.assertNotIn("something else is at skills/xsm", text)
        from xsm import install
        snap = install.runtime_current()
        self.assertEqual(os.path.realpath(self._cli_link()),
                         os.path.realpath(os.path.join(snap["path"], "bin", "xsm")))
        self.assertEqual(os.path.realpath(os.path.join(home, "skills", "xsm")),
                         os.path.realpath(os.path.join(snap["path"], "skills", "xsm")))


class PruneTest(_Checkout):
    def _two(self):
        from xsm import install
        first = install.make_snapshot()
        self._edit()
        return first, install.make_snapshot()

    def test_the_previous_one_stays_while_a_session_that_started_before_it_lives(self):
        from xsm import install
        first, second = self._two()
        with mock.patch.object(install, "_running_from", return_value=set()), \
                mock.patch.object(install, "_live_session_starts",
                                  return_value=[time.time() - 3600]):
            self.assertEqual(install.prune_snapshots(), [])
        self.assertTrue(os.path.isdir(first["path"]))

    def test_it_goes_when_no_session_is_older_than_the_swap_and_nothing_runs_from_it(self):
        from xsm import install
        first, second = self._two()
        with mock.patch.object(install, "_running_from", return_value=set()), \
                mock.patch.object(install, "_live_session_starts",
                                  return_value=[time.time() + 3600]):
            self.assertEqual(install.prune_snapshots(), [first["id"]])
        self.assertFalse(os.path.exists(first["path"]))
        self.assertTrue(os.path.isdir(second["path"]), "the installed one never goes")
        self.assertEqual(os.path.realpath(os.path.join(self.tmp, "runtime", "current")),
                         os.path.realpath(second["path"]))

    def test_a_skill_link_that_names_it_keeps_it(self):
        """A plain install leaves a home's skill link where it was; the snapshot
        it names must not be pruned under it (review of PR #10)."""
        from xsm import config, install
        first, second = self._two()
        home = os.path.join(self.tmp, "claude-skill")
        os.makedirs(os.path.join(home, "skills"))
        os.symlink(os.path.join(first["path"], "skills", "xsm"),
                   os.path.join(home, "skills", "xsm"))
        config.add_home(home, "claude")
        with mock.patch.object(install, "_running_from", return_value=set()), \
                mock.patch.object(install, "_live_session_starts", return_value=[]):
            self.assertEqual(install.prune_snapshots(), [])
        self.assertTrue(os.path.isdir(first["path"]))

    def test_a_running_process_or_a_config_that_names_it_keeps_it(self):
        from xsm import config, install, paths
        first, second = self._two()
        starts = mock.patch.object(install, "_live_session_starts", return_value=[])
        with starts, mock.patch.object(install, "_running_from", return_value={first["id"]}):
            self.assertEqual(install.prune_snapshots(), [])
        home = os.path.join(self.tmp, "claude-keeps")
        paths.write_json(os.path.join(home, "settings.json"), {"hooks": {"SessionEnd": [{"hooks": [
            {"type": "command", "command": '"%s" %s' % (
                os.path.join(first["path"], "hooks", "xsm-hook"), install.MARKER)}]}]}})
        config.add_home(home, "claude")
        with starts, mock.patch.object(install, "_running_from", return_value=set()):
            self.assertEqual(install.prune_snapshots(), [], "a home not refreshed keeps its copy")
            paths.write_json(os.path.join(home, "settings.json"), {})
            self.assertEqual(install.prune_snapshots(), [first["id"]])

    def test_an_unfinished_copy_is_swept_and_a_fresh_one_is_left(self):
        from xsm import install
        install.make_snapshot()
        stale = os.path.join(self.tmp, "runtime", "0123456789ab.new")
        fresh = os.path.join(self.tmp, "runtime", "ba9876543210.new")
        os.makedirs(stale)
        os.makedirs(fresh)
        old = time.time() - 3600
        os.utime(stale, (old, old))
        with mock.patch.object(install, "_running_from", return_value=set()):
            install.prune_snapshots()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))


class DoctorRuntimeTest(_Checkout):
    def _doctor(self):
        code, text = _run_cli("doctor")
        return [l for l in text.splitlines() if l.startswith("runtime ")]

    def test_before_an_install_it_says_the_hooks_run_from_the_checkout(self):
        lines = self._doctor()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("no copy installed", lines[0])
        self.assertIn("xsm install --refresh", lines[0])

    def test_it_names_the_snapshot_and_a_checkout_that_moved_on(self):
        from xsm import install
        snap = install.make_snapshot()
        lines = self._doctor()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("snapshot %s" % snap["id"], lines[0])
        self.assertIn(self.checkout.replace(self.user_home, "~"), lines[0])
        self._edit()
        lines = self._doctor()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("different from the copy", lines[1])
        self.assertIn("your agent can update it by running `%s install --refresh`" % os.path.join(
            self.checkout, "bin", "xsm"), lines[1])
        self.assertNotIn("type", lines[1])
        report = install.doctor()["runtime"]
        self.assertFalse(report["checkout"]["same"])
        self.assertEqual(report["snapshot"]["id"], snap["id"])
        install.make_snapshot()
        self.assertEqual(len(self._doctor()), 1)

    def test_it_counts_the_commits_when_the_checkout_is_a_git_repository(self):
        from xsm import install
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "a"]):
            subprocess.run(["git", "-C", self.checkout] + args, check=True, capture_output=True)
        install.make_snapshot()
        self._edit()
        subprocess.run(["git", "-C", self.checkout, "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.checkout, "-c", "user.name=t", "-c",
                        "user.email=t@example.invalid", "commit", "-qm", "b"],
                       check=True, capture_output=True)
        self.assertIn("1 commit(s) ahead of the copy", self._doctor()[1])

    def test_a_checkout_that_is_gone_is_said_and_the_copy_keeps_working(self):
        from xsm import install
        install.make_snapshot()
        with mock.patch.object(install, "REPO", os.path.join(self.tmp, "elsewhere")):
            shutil.rmtree(self.checkout)
            lines = self._doctor()
        self.assertTrue(any("is gone; the copy keeps working" in l for l in lines), lines)

    def test_the_checkout_policy_is_named_with_what_it_costs(self):
        with mock.patch.dict(os.environ, {"XSM_RUNTIME": "checkout"}):
            lines = self._doctor()
        self.assertIn("a session macOS keeps out of that folder cannot run them", lines[0])
        self.assertIn("delete the `runtime` line", lines[0], "the way back is named")


class LauncherPinTest(TempState):
    """B2/B5: install writes the pin as `interpreter`; the launchers read both names."""

    def _tree(self, launcher, script, body):
        tree = tempfile.mkdtemp(dir=self.tmp)
        os.makedirs(os.path.join(tree, "hooks"))
        shutil.copy(os.path.join(REPO, "hooks", launcher), os.path.join(tree, "hooks", launcher))
        with open(os.path.join(tree, "hooks", script), "w") as fh:
            fh.write(body)
        return tree

    def _bare_path(self):
        """A PATH with the tools the launcher uses and no python at all, so only a
        pin can find the interpreter."""
        bin_dir = os.path.join(self.tmp, "bare-bin")
        os.makedirs(bin_dir, exist_ok=True)
        for tool in ("cat", "sed", "head", "dirname", "sh"):
            found = shutil.which(tool)
            if found and not os.path.exists(os.path.join(bin_dir, tool)):
                os.symlink(found, os.path.join(bin_dir, tool))
        return bin_dir

    def _run(self, launcher, script, name):
        tree = self._tree(launcher, script, "print('ran under the pin')\n")
        from xsm import paths
        pin = {"path": os.path.realpath(sys.executable), "version": "x"}
        paths.write_json(os.path.join(self.tmp, name), pin)
        env = dict(os.environ, XSM_HOME=self.tmp, PATH=self._bare_path())
        env.pop("XSM_PYTHON", None)
        env.pop("XSM_PYTHON_CANDIDATES", None)
        return subprocess.run([os.path.join(tree, "hooks", launcher)], input="{}",
                              capture_output=True, text=True, env=env)

    def test_the_pin_install_writes_is_read_under_either_name(self):
        from xsm import install
        self.assertEqual(install.INTERPRETER, "interpreter", "what install writes")
        for launcher, script in (("xsm-hook", "xsm-hook.py"), ("xsm-mcp", "xsm-mcp.py")):
            for name in ("interpreter", "interpreter.json"):
                with self.subTest(launcher=launcher, name=name):
                    out = self._run(launcher, script, name)
                    self.assertEqual((out.returncode, out.stdout.strip()),
                                     (0, "ran under the pin"), out.stderr)
                os.unlink(os.path.join(self.tmp, name))


class StatuslineRuntimeTest(_Checkout):
    def test_a_refresh_points_xsms_own_statusline_at_the_runtime_now_installed(self):
        from xsm import install, paths
        home = os.path.join(self.tmp, "claude-line")
        os.makedirs(home)
        paths.write_json(os.path.join(home, "settings.json"),
                         {"statusLine": {"type": "command", "command": "mine --dash",
                                         "refreshInterval": 5}})
        install.make_snapshot()
        self.assertEqual(install.install_statusline(home), "composed")
        target = os.path.join(home, "settings.json")
        first = paths.read_json(target)["statusLine"]["command"]
        self.assertIn(install.runtime_current()["path"], first)
        self.assertIn("--base", first)
        self.assertFalse(install.refresh_statusline(home), "already right")
        self._edit()
        install.make_snapshot()
        self.assertTrue(install.refresh_statusline(home))
        line = paths.read_json(target)["statusLine"]
        self.assertEqual(line["refreshInterval"], 5)
        self.assertIn(install.runtime_current()["path"], line["command"])
        self.assertIn("--base", line["command"], "still composed on the person's own")
        self.assertEqual(install.statusline_base(target)["command"], "mine --dash")

    def test_a_statusline_that_is_not_xsms_is_never_touched(self):
        from xsm import install, paths
        home = os.path.join(self.tmp, "claude-line-theirs")
        os.makedirs(home)
        paths.write_json(os.path.join(home, "settings.json"),
                         {"statusLine": {"type": "command", "command": "mine"}})
        self.assertFalse(install.refresh_statusline(home))


class PluginHooksTest(unittest.TestCase):
    def test_the_plugins_timeouts_are_the_installers(self):
        """B8: a PermissionRequest hook that waits for a person was killed at
        10 s in the plugin and lived 660 s in a direct install. hooks/hooks.json is
        the Claude plugin's; Codex's trust covers hooks/codex-hooks.json only."""
        from xsm import install
        with open(os.path.join(REPO, "hooks", "hooks.json")) as fh:
            hooks = json.load(fh)["hooks"]
        for event, groups in hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertEqual(hook["timeout"], install.TIMEOUTS.get(event, 10), event)
        self.assertEqual(hooks["PermissionRequest"][0]["hooks"][0]["timeout"], 660)

    def test_no_codex_manifest_names_hooks_json(self):
        with open(os.path.join(REPO, ".codex-plugin", "plugin.json")) as fh:
            self.assertEqual(json.load(fh)["hooks"], "./hooks/codex-hooks.json")
        with open(os.path.join(REPO, ".claude-plugin", "plugin.json")) as fh:
            self.assertEqual(json.load(fh)["hooks"], "./hooks/hooks.json")


if __name__ == "__main__":
    unittest.main()
