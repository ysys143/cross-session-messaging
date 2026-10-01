"""Which xsm is this? A plugin, a checkout and sessions started before an update
can all be on one machine (2026-10-01), so `xsm --version`, doctor's `cli` line
and its `plugin` lines say which version runs where. Tests pin the format, never
this machine's paths."""
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_xsm  # noqa: E402

# Module names only: a class imported here by name would run its tests twice.
TempState = test_xsm.TempState
REPO = test_xsm.REPO


class VersionTest(TempState):
    def _run(self, argv):
        from xsm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def _version(self):
        from xsm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            cli.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        return out.getvalue()

    def test_version_says_the_version_the_describe_and_where_it_runs_from(self):
        from xsm import install
        with mock.patch.object(install, "git_describe", return_value="v0.4.14-5-gabc1234"):
            text = self._version()
        self.assertEqual(text, "xsm %s (git v0.4.14-5-gabc1234) at %s\n" % (
            install.plugin_version(), os.path.realpath(install.launcher())))
        self.assertRegex(text, r"\Axsm \d+\.\d+\.\d+ \(git \S+\) at /\S*bin/xsm\n\Z")
        with mock.patch.object(install, "git_describe", return_value=None):
            text = self._version()
        self.assertRegex(text, r"\Axsm \d+\.\d+\.\d+ at /\S*bin/xsm\n\Z")
        self.assertNotIn("git", text)

    def test_version_needs_no_command_and_touches_no_state(self):
        from xsm import paths
        never_made = os.path.join(self.tmp, "never-made")
        paths.HOME = never_made
        with mock.patch.dict(os.environ, {"XSM_HOME": never_made}):
            self._version()
        self.assertFalse(os.path.exists(never_made))

    def test_the_describe_comes_from_a_checkout_only_and_never_hangs(self):
        from xsm import install
        git = lambda *a: subprocess.run(
            ("git", "-C", self.tmp, "-c", "user.email=t@t", "-c", "user.name=t") + a,
            check=True, capture_output=True)
        with mock.patch.object(install, "REPO", self.tmp):
            self.assertIsNone(install.git_describe(), "a plugin copy has no .git")
            git("init", "-q")
            git("commit", "-q", "--allow-empty", "-m", "one")
            git("tag", "v1.0.0")
            self.assertEqual(install.git_describe(), "v1.0.0")
            git("commit", "-q", "--allow-empty", "-m", "two")
            self.assertRegex(install.git_describe(), r"\Av1\.0\.0-1-g[0-9a-f]+\Z")
            with mock.patch.object(install.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired("git", 2)):
                self.assertIsNone(install.git_describe())
            with mock.patch.object(install.subprocess, "run", side_effect=OSError("no git")):
                self.assertIsNone(install.git_describe())


class DoctorLinesTest(TempState):
    def _doctor(self, describe=None, path=None):
        from xsm import cli, install
        out = io.StringIO()
        env = {"PATH": path} if path is not None else {}
        with mock.patch.object(install, "git_describe", return_value=describe), \
                mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            cli.main(["doctor"])
        return out.getvalue().splitlines()

    def _codex_home(self, version):
        from xsm import config
        home = os.path.join(self.tmp, "codex-home")
        root = os.path.join(home, "plugins", "cache", "xsm", "xsm", version)
        os.makedirs(os.path.join(root, ".codex-plugin"))
        with open(os.path.join(home, "config.toml"), "w") as fh:
            fh.write('[plugins."xsm@xsm"]\n')
        config.add_home(home, "codex")
        return os.path.realpath(home), os.path.realpath(root)

    def _claude_home(self, version):
        from xsm import config, paths
        home = os.path.join(self.tmp, "claude-home")
        root = os.path.join(home, "plugins", "cache", "xsm", "xsm", version)
        os.makedirs(os.path.join(root, "hooks"))
        shutil.copy(os.path.join(REPO, "hooks", "hooks.json"), os.path.join(root, "hooks"))
        paths.write_json(os.path.join(home, "plugins", "installed_plugins.json"), {
            "version": 2, "plugins": {"xsm@xsm": [{"version": version, "installPath": root}]}})
        config.add_home(home, "claude")
        return os.path.realpath(home), root

    def test_the_cli_line_names_this_cli_its_version_and_describe(self):
        from xsm import install
        lines = self._doctor("v1.2.3-4-gabc1234")
        cli_lines = [l for l in lines if l.startswith("cli ")]
        self.assertEqual(len(cli_lines), 1, cli_lines)
        self.assertEqual(cli_lines[0], "cli        xsm %s (git v1.2.3-4-gabc1234) at %s" % (
            install.plugin_version(), os.path.realpath(install.launcher())))
        self.assertRegex(cli_lines[0], r"\Acli {8}xsm \d+\.\d+\.\d+ \(git \S+\) at /\S+\Z")
        self.assertNotIn("also on PATH", "\n".join(lines))

    def test_every_other_xsm_on_path_is_named_with_its_version(self):
        other = os.path.join(self.tmp, "other-checkout")
        os.makedirs(os.path.join(other, "bin"))
        os.makedirs(os.path.join(other, ".claude-plugin"))
        launcher = os.path.join(other, "bin", "xsm")
        with open(launcher, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(launcher, 0o755)
        with open(os.path.join(other, ".claude-plugin", "plugin.json"), "w") as fh:
            fh.write('{"version": "0.4.9"}')
        link_dir = os.path.join(self.tmp, "linkbin")
        os.makedirs(link_dir)
        os.symlink(launcher, os.path.join(link_dir, "xsm"))
        lines = self._doctor(None, os.pathsep.join([link_dir, os.environ["PATH"]]))
        also = [l for l in lines if "also on PATH" in l]
        self.assertEqual(also, ["cli        also on PATH as %s: xsm 0.4.9 at %s" % (
            os.path.join(link_dir, "xsm"), os.path.realpath(launcher))])

    def test_a_plugin_line_carries_the_install_root(self):
        home, root = self._codex_home("0.4.14")
        from xsm import install
        line = next(l for l in self._doctor("v0.4.14") if l.startswith("plugin "))
        self.assertEqual(line, "plugin     %-45s xsm 0.4.14 at %s" % (home, root))
        self.assertEqual(install.plugin_root(home), root)
        self.assertTrue(os.path.isabs(root), "`<root>/bin/xsm` runs that version by path")
        claude_home, claude_root = self._claude_home("0.4.14")
        line = next(l for l in self._doctor("v0.4.14")
                    if l.startswith("plugin ") and "claude-home" in l)
        self.assertEqual(line, "plugin     %-45s xsm 0.4.14 at %s" % (claude_home, claude_root))

    def test_a_plugin_older_than_this_cli_says_so(self):
        from xsm import install
        home, root = self._codex_home("0.4.9")
        line = next(l for l in self._doctor("v0.4.14") if l.startswith("plugin "))
        self.assertRegex(line, r"\Aplugin {5}\S+ +xsm 0\.4\.9 at /\S+  older than this CLI "
                               r"\(%s\)\Z" % re.escape(install.plugin_version()))
        claude_home, _ = self._claude_home("0.4.9")
        line = next(l for l in self._doctor("v0.4.14")
                    if l.startswith("plugin ") and "claude-home" in l)
        self.assertIn("older than this CLI (%s)" % install.plugin_version(), line)

    def test_a_newer_plugin_is_not_called_older(self):
        self._codex_home("9.9.9")
        line = next(l for l in self._doctor("v0.4.14") if l.startswith("plugin "))
        self.assertNotIn("older than this CLI", line)

    def test_the_same_version_is_older_only_when_this_cli_is_past_its_tag(self):
        from xsm import install
        mine = install.plugin_version()
        home, root = self._codex_home(mine)
        line = next(l for l in self._doctor("v%s" % mine) if l.startswith("plugin "))
        self.assertNotIn("older than this CLI", line, "at its tag: the same code")
        line = next(l for l in self._doctor("v%s-5-gabc1234" % mine) if l.startswith("plugin "))
        self.assertIn("may be older than this CLI (%s, which is past its tag: v%s-5-gabc1234)"
                      % (mine, mine), line)
        line = next(l for l in self._doctor(None) if l.startswith("plugin "))
        self.assertNotIn("older than this CLI", line, "no describe, nothing to compare")

    def test_the_report_carries_what_the_lines_are_made_of(self):
        from xsm import install
        home, root = self._codex_home("0.4.9")
        with mock.patch.object(install, "git_describe", return_value="v0.4.14-1-gabc1234"):
            report = install.doctor()
        self.assertEqual(report["cli"]["describe"], "v0.4.14-1-gabc1234")
        self.assertEqual(report["cli"]["path"], os.path.realpath(install.launcher()))
        self.assertEqual(report["cli"]["version"], install.plugin_version())
        self.assertEqual(report["plugin_roots"][home], root)
        self.assertIn("older than this CLI", report["plugin_older"][home])

    def test_the_table_view_has_the_cli_row_and_the_older_plugin(self):
        from xsm import cli, install
        home, root = self._codex_home("0.4.9")
        with mock.patch.object(install, "git_describe", return_value=None):
            report = install.doctor()
        rows = cli._doctor_rows(report)
        self.assertEqual(rows[0][0], "cli")
        self.assertTrue(rows[0][1].startswith("xsm %s at " % install.plugin_version()))
        self.assertIn(("plugin", "%s: xsm 0.4.9 at %s, older than this CLI (%s)" % (
            home, root, install.plugin_version())), rows)


class PluginOlderTest(TempState):
    def test_only_a_lower_version_or_a_checkout_past_its_tag_counts(self):
        from xsm import install
        older = install.plugin_older
        with mock.patch.object(install, "plugin_version", return_value="0.4.14"):
            self.assertEqual(older("0.4.9", "/p/0.4.9", None), "older than this CLI (0.4.14)")
            self.assertEqual(older("0.4.13", "/p", "v0.4.14"), "older than this CLI (0.4.14)")
            self.assertIsNone(older("0.4.14", "/p", "v0.4.14"))
            self.assertIsNone(older("0.4.14", "/p", None))
            self.assertIsNone(older("0.4.15", "/p", "v0.4.14-3-gabc"))
            self.assertIn("may be older", older("0.4.14", "/p", "v0.4.14-3-gabc"))
            self.assertIn("may be older", older("0.4.14", "/p", "v0.4.14-3-gabc-dirty"))
            self.assertIsNone(older("0.4.14", install.REPO, "v0.4.14-3-gabc"),
                              "this checkout is not older than itself")
            self.assertIsNone(older("installed", "/p", "v0.4.14-3-gabc"), "no version to compare")
            self.assertIsNone(older(None, "/p", None))
        with mock.patch.object(install, "plugin_version", return_value="0.4.10"):
            self.assertEqual(older("0.4.9", None, None), "older than this CLI (0.4.10)",
                             "by number, as version_key sorts")
            self.assertIsNone(older("0.4.10", None, "v0.4.10"))


if __name__ == "__main__":
    unittest.main()
