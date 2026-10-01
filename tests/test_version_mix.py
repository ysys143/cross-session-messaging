"""Which xsm is this? A plugin, a checkout and sessions started before an update
can all be on one machine (2026-10-01), so `xsm --version`, doctor's `cli` line
and its `plugin` lines say which version runs where. Tests pin the format, never
this machine's paths."""
import contextlib
import io
import json
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
# A runtime file of the fake checkouts below, as text by path.
RUNTIME = {"xsm/a.py": "a = 1\n", "hooks/hooks.json": "{}\n", "skills/xsm/SKILL.md": "skill\n",
           "bin/xsm": "#!/bin/sh\n"}


def git(folder, *args):
    return subprocess.run(
        ("git", "-C", folder, "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false") + args, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(root, files):
    for rel, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
        with open(os.path.join(root, rel), "w") as fh:
            fh.write(text)


def make_checkout(tmp, name="checkout"):
    """A fake checkout of this CLI with four commits: (folder, [commit, ...] oldest
    first). Small, so a test that asks git stays fast."""
    repo = os.path.join(tmp, name)
    write(repo, dict(RUNTIME, **{".claude-plugin/plugin.json": '{"version": "0.4.14"}'}))
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    revs = []
    for n in range(4):
        git(repo, "commit", "-q", "--allow-empty", "-m", "c%d" % n)
        revs.append(git(repo, "rev-parse", "HEAD"))
    return repo, revs


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
        # The developer's own PATH may hold another xsm (~/.local/bin/xsm); the
        # lines must not depend on it.
        env = {"PATH": path if path is not None else os.pathsep.join(
            (os.path.dirname(sys.executable), "/usr/bin", "/bin"))}
        with mock.patch.object(install, "git_describe", return_value=describe), \
                mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            cli.main(["doctor"])
        return out.getvalue().splitlines()

    def _codex_home(self, version, name="codex-home", revision=None):
        """A Codex home with the plugin cached; `revision` is what Codex's
        marketplace install records for it (.codex-marketplace-install.json)."""
        from xsm import config
        home = os.path.join(self.tmp, name)
        root = os.path.join(home, "plugins", "cache", "xsm", "xsm", version)
        os.makedirs(os.path.join(root, ".codex-plugin"))
        with open(os.path.join(home, "config.toml"), "w") as fh:
            fh.write('[plugins."xsm@xsm"]\n')
        if revision:
            write(root, {".codex-marketplace-install.json": json.dumps(
                {"source_type": "git", "ref_name": None, "revision": revision})})
        config.add_home(home, "codex")
        return os.path.realpath(home), os.path.realpath(root)

    def _claude_home(self, version, source=REPO):
        """A Claude home with the plugin copied in: the runtime files of
        `source`, no .git and no recorded commit (a Claude plugin copy)."""
        import glob
        from xsm import config, install, paths
        home = os.path.join(self.tmp, "claude-home")
        root = os.path.join(home, "plugins", "cache", "xsm", "xsm", version)
        for pattern in install.RUNTIME_FILES:
            for name in glob.glob(os.path.join(source, pattern), recursive=True):
                if os.path.isfile(name) and "__pycache__" not in name:
                    target = os.path.join(root, os.path.relpath(name, source))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy(name, target)
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
        lines = self._doctor(None, os.pathsep.join([link_dir, os.path.dirname(sys.executable),
                                                     "/usr/bin", "/bin"]))
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

    def test_a_cache_is_judged_by_its_own_commit_not_by_this_cli_being_past_its_tag(self):
        """Three Codex caches of the same version: one at this CLI's commit, one
        behind it, one at a commit this checkout does not have (2026-10-01: the
        first was called older because this CLI is a checkout past its tag)."""
        from xsm import install
        repo, revs = make_checkout(self.tmp)
        homes = {name: self._codex_home("0.4.14", name, rev) for name, rev in (
            ("c-same", revs[3]), ("c-behind", revs[1]), ("c-last", revs[2]),
            ("c-unknown", "e" * 40))}
        with mock.patch.object(install, "REPO", repo):
            lines = self._doctor("v0.4.14-3-g%s" % revs[3][:7])
        line = {name: next(l for l in lines if l.startswith("plugin ") and home[0] in l)
                for name, home in homes.items()}
        self.assertNotIn("CLI", line["c-same"], "the same commit: no flag")
        self.assertTrue(line["c-same"].endswith(homes["c-same"][1]), line["c-same"])
        self.assertTrue(line["c-behind"].endswith(
            "  2 commits behind this CLI (%s)" % revs[1][:7]), line["c-behind"])
        self.assertTrue(line["c-last"].endswith(
            "  1 commit behind this CLI (%s)" % revs[2][:7]), line["c-last"])
        self.assertTrue(line["c-unknown"].endswith("  differs from this CLI (eeeeeee)"),
                        line["c-unknown"])
        self.assertNotIn("older than this CLI", "\n".join(lines), "never `older` without evidence")

    def test_a_claude_copy_with_no_commit_is_judged_by_its_runtime_files(self):
        from xsm import install
        repo, _ = make_checkout(self.tmp)
        home, root = self._claude_home("0.4.14", repo)
        with mock.patch.object(install, "REPO", repo):
            line = next(l for l in self._doctor("v0.4.14-3-gabc1234") if l.startswith("plugin "))
        self.assertEqual(line, "plugin     %-45s xsm 0.4.14 at %s" % (home, root),
                         "byte for byte this CLI's runtime: no flag, though it has no history")
        write(root, {"xsm/a.py": "a = 2\n"})
        with mock.patch.object(install, "REPO", repo):
            line = next(l for l in self._doctor("v0.4.14-3-gabc1234") if l.startswith("plugin "))
        self.assertTrue(line.endswith("  differs from this CLI"), line)
        self.assertNotIn("older than this CLI", line)

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

    def test_the_report_carries_the_commits_the_comparison_used(self):
        from xsm import install
        repo, revs = make_checkout(self.tmp)
        home, root = self._codex_home("0.4.14", "codex-a", revs[0])
        claude_home, _ = self._claude_home("0.4.14", repo)
        with mock.patch.object(install, "REPO", repo):
            report = install.doctor()
        self.assertEqual(report["cli"]["revision"], revs[3])
        self.assertEqual(report["plugin_revisions"], {home: revs[0], claude_home: None})
        self.assertEqual(report["plugin_older"][home],
                         "3 commits behind this CLI (%s)" % revs[0][:7])
        self.assertIsNone(report["plugin_older"][claude_home])

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


class PluginCommitTest(TempState):
    """plugin_older against a fake checkout of this CLI (four commits)."""
    def setUp(self):
        super().setUp()
        from xsm import install
        self.repo, self.revs = make_checkout(self.tmp)
        for patch in (mock.patch.object(install, "REPO", self.repo),
                      mock.patch.object(install, "plugin_version", return_value="0.4.14")):
            patch.start()
            self.addCleanup(patch.stop)

    def _older(self, version, revision, head=None, root="/p"):
        from xsm import install
        return install.plugin_older(version, root, revision, head or self.revs[3])

    def test_a_lower_version_is_older_and_a_higher_one_is_not(self):
        self.assertEqual(self._older("0.4.9", None), "older than this CLI (0.4.14)")
        self.assertEqual(self._older("0.4.13", self.revs[3]), "older than this CLI (0.4.14)",
                         "by version, whatever the commit says")
        self.assertIsNone(self._older("0.4.15", self.revs[0]))
        self.assertIsNone(self._older("installed", self.revs[0]), "no version to compare")
        self.assertIsNone(self._older(None, None))

    def test_the_same_commit_is_the_same_code(self):
        self.assertIsNone(self._older("0.4.14", self.revs[3]))
        self.assertIsNone(self._older("0.4.14", self.revs[3][:9]), "an abbreviated one too")
        from xsm import install
        self.assertIsNone(install.plugin_older("0.4.14", self.repo, self.revs[0], self.revs[3]),
                          "this checkout is not older than itself")

    def test_a_copy_behind_says_by_how_many_commits(self):
        self.assertEqual(self._older("0.4.14", self.revs[1]),
                         "2 commits behind this CLI (%s)" % self.revs[1][:7])
        self.assertEqual(self._older("0.4.14", self.revs[2]),
                         "1 commit behind this CLI (%s)" % self.revs[2][:7])

    def test_a_commit_that_is_no_ancestor_or_not_known_here_only_differs(self):
        from xsm import install
        git(self.repo, "checkout", "-q", "-b", "side", self.revs[1])
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "side")
        side = git(self.repo, "rev-parse", "HEAD")
        calls = []
        real = install._git
        with mock.patch.object(install, "_git",
                               side_effect=lambda *a: calls.append(a) or real(*a)):
            self.assertEqual(self._older("0.4.14", side),
                             "differs from this CLI (%s)" % side[:7], "another branch")
            self.assertEqual(self._older("0.4.14", self.revs[3], self.revs[1]),
                             "differs from this CLI (%s)" % self.revs[3][:7],
                             "a copy ahead of this CLI is not behind it")
            self.assertEqual(self._older("0.4.14", "f" * 40), "differs from this CLI (fffffff)",
                             "unknown here")
        self.assertFalse([c for c in calls if "fetch" in c], "nothing is fetched to find out")

    def test_a_copy_with_no_commit_is_compared_by_its_runtime_files(self):
        import shutil
        mine = os.path.join(self.tmp, "mine")
        copy = os.path.join(self.tmp, "copy")
        write(mine, RUNTIME)
        self.addCleanup(shutil.rmtree, mine, True)
        shutil.copytree(mine, copy)
        from xsm import install
        with mock.patch.object(install, "REPO", mine):
            self.assertIsNone(self._older("0.4.14", None, root=copy), "the same bytes")
            write(copy, {"README.md": "x", "tests/t.py": "t", "xsm/__pycache__/a.pyc": "p",
                         "docs/d.md": "d"})
            self.assertIsNone(self._older("0.4.14", None, root=copy), "only the runtime counts")
            for rel in ("xsm/a.py", "hooks/hooks.json", "skills/xsm/SKILL.md", "bin/xsm"):
                with self.subTest(rel):
                    write(copy, {rel: "changed\n"})
                    self.assertEqual(self._older("0.4.14", None, root=copy),
                                     "differs from this CLI")
                    shutil.copy(os.path.join(mine, rel), os.path.join(copy, rel))
            write(copy, {"skills/xsm/references/new.md": "a new reference\n"})
            self.assertEqual(self._older("0.4.14", None, root=copy), "differs from this CLI",
                             "a file the other lacks")
            nothing = os.path.join(self.tmp, "nothing")
            os.makedirs(nothing)
            self.assertIsNone(self._older("0.4.14", None, root=nothing),
                              "no runtime files: nothing to say")
            # a commit this CLI cannot compare (it is not a checkout) falls back to the files
            self.assertEqual(install.plugin_older("0.4.14", copy, self.revs[1], None),
                             "differs from this CLI")

    def test_the_revision_is_the_one_codex_recorded_else_the_clones_head(self):
        from xsm import install
        root = os.path.join(self.tmp, "cache", "0.4.14")
        os.makedirs(root)
        self.assertIsNone(install.plugin_revision(root), "no record, no .git")
        self.assertIsNone(install.plugin_revision(None))
        record = os.path.join(root, ".codex-marketplace-install.json")
        write(root, {".codex-marketplace-install.json": json.dumps({"revision": self.revs[1]})})
        self.assertEqual(install.plugin_revision(root), self.revs[1])
        for bad in ("--upload-pack=x", "", "xyz", 7, None):
            with self.subTest(repr(bad)):
                write(root, {".codex-marketplace-install.json": json.dumps({"revision": bad})})
                self.assertIsNone(install.plugin_revision(root), "never reaches a git command")
        with open(record, "w") as fh:
            fh.write("{ not json")
        self.assertIsNone(install.plugin_revision(root))
        git(self.tmp, "clone", "-q", self.repo, os.path.join(self.tmp, "clone"))
        clone = os.path.join(self.tmp, "clone")
        self.assertEqual(install.plugin_revision(clone), self.revs[3], "a clone's own HEAD")
        inner = os.path.join(self.repo, "plugins", "cache", "0.4.14")
        os.makedirs(inner)
        self.assertIsNone(install.plugin_revision(inner),
                          "a copy inside a repository is not that repository's HEAD")

    def test_git_that_hangs_or_is_missing_says_nothing(self):
        from xsm import install
        for error in (subprocess.TimeoutExpired("git", 2), OSError("no git")):
            with self.subTest(type(error).__name__), \
                    mock.patch.object(install.subprocess, "run", side_effect=error):
                self.assertIsNone(install.git_head(self.repo))
                self.assertEqual(self._older("0.4.14", self.revs[1]),
                                 "differs from this CLI (%s)" % self.revs[1][:7])


if __name__ == "__main__":
    unittest.main()
