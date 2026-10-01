"""`xsm list` must not wait for a session's folder (2026-10-02, the Mac mini).

It read each session's `<cwd>/.claude/settings*.json` and ran `git rev-parse` per
folder, 42 git calls for 8 folders, with no limit that holds: one folder under
~/Documents whose opendir never returned froze `list` and `list -a` for good. A probe
that does not answer in a second is unknown, a folder is asked once, and a folder that
did not answer is not asked again."""
import contextlib
import io
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_xsm  # noqa: E402

# Module names only: a class imported here by name would run its tests twice.
TempState = test_xsm.TempState


class _StuckGit:
    """A git that is in a read that never returns: communicate waits out its timeout,
    and kill changes nothing (the child is not interruptible)."""

    killed = 0

    def __init__(self, *args, **kwargs):
        self.stdout = io.StringIO()
        self.returncode = None

    def communicate(self, timeout=None):
        time.sleep(timeout)
        raise subprocess.TimeoutExpired("git", timeout)

    def kill(self):
        type(self).killed += 1


class BoundedTest(unittest.TestCase):
    def test_a_fast_answer_is_the_answer_and_a_raise_is_unknown(self):
        from xsm import probe
        self.assertEqual(probe.bounded(lambda: 7), 7)
        self.assertIsNone(probe.bounded(lambda: {}["nope"]))
        self.assertEqual(probe.bounded(lambda: 1 / 0, default="unknown"), "unknown")

    def test_a_read_that_never_returns_costs_the_deadline_and_no_more(self):
        from xsm import probe
        began = time.monotonic()
        self.assertEqual(probe.bounded(time.sleep, 30, timeout=0.2, default="unknown"), "unknown")
        self.assertLess(time.monotonic() - began, 2)

    def test_the_ordinary_deadline_is_read_when_it_is_asked_not_when_it_is_defined(self):
        from xsm import probe
        with mock.patch.object(probe, "READ_TIMEOUT", 0.2):
            began = time.monotonic()
            self.assertIsNone(probe.bounded(time.sleep, 30))
        self.assertLess(time.monotonic() - began, 2)

    def test_inside_quick_a_folder_that_did_not_answer_is_not_asked_again(self):
        from xsm import probe
        calls = []

        def slow():
            calls.append(1)
            time.sleep(30)

        with probe.quick(0.2):
            began = time.monotonic()
            for _ in range(5):
                probe.bounded(slow, folder="/stalled")
            self.assertEqual(len(calls), 1)
            self.assertLess(time.monotonic() - began, 1.5)
            self.assertEqual(probe.bounded(lambda: "ok", folder="/other"), "ok")
        # Outside it nothing is remembered: a folder that stalled once may answer later.
        self.assertEqual(probe.bounded(lambda: "ok", folder="/stalled"), "ok")

    def test_quick_caps_a_longer_deadline_and_remembers_one_answer_per_question(self):
        from xsm import probe
        asked = []
        with probe.quick(0.2):
            self.assertEqual(probe.limit(5.0), 0.2)
            for _ in range(4):
                probe.remember(("k", 1), lambda: asked.append(1) or "answer")
        self.assertEqual(len(asked), 1)
        self.assertEqual(probe.limit(5.0), 5.0, "and nothing is capped outside it")
        for _ in range(2):
            probe.remember(("k", 1), lambda: asked.append(1))
        self.assertEqual(len(asked), 3, "outside quick every call asks again")


class GitTest(TempState):
    def test_a_git_that_does_not_answer_is_killed_not_waited_for_and_unknown(self):
        from xsm import probe
        _StuckGit.killed = 0
        with mock.patch.object(probe.subprocess, "Popen", _StuckGit):
            began = time.monotonic()
            self.assertIsNone(probe.git("/stalled", "rev-parse", timeout=0.2))
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertEqual(_StuckGit.killed, 1)

    def test_inside_quick_the_folder_that_stalled_costs_one_deadline_not_one_per_question(self):
        from xsm import probe
        _StuckGit.killed = 0
        with mock.patch.object(probe.subprocess, "Popen", _StuckGit):
            with probe.quick(0.2):
                began = time.monotonic()
                for _ in range(6):
                    self.assertIsNone(probe.git("/stalled", "rev-parse"))
                self.assertLess(time.monotonic() - began, 1.5)
            self.assertEqual(_StuckGit.killed, 1)

    def test_a_missing_git_or_a_failed_one_is_unknown_and_a_good_one_is_its_output(self):
        from xsm import probe
        with mock.patch.object(probe.subprocess, "Popen", side_effect=FileNotFoundError("git")):
            self.assertIsNone(probe.git("/x", "status"))
        self.assertIsNone(probe.git(os.path.join(os.sep, "nonexistent-folder-for-xsm"), "status"))
        folder = os.path.join(self.tmp, "a-repository")
        os.makedirs(folder)
        subprocess.run(["git", "init", "-q", folder], check=True)
        self.assertEqual(probe.git(folder, "rev-parse", "--is-inside-work-tree"), "true\n")


class FoldersTest(TempState):
    """A repository's top and a session's settings, asked once and never waited on."""

    def _repo(self, name):
        folder = os.path.realpath(os.path.join(self.tmp, "work", name))
        os.makedirs(folder)
        subprocess.run(["git", "init", "-q", folder], check=True)
        return folder

    def test_git_repo_is_asked_once_per_folder_inside_quick_and_each_time_outside(self):
        from xsm import config, probe
        folder = self._repo("one")
        with mock.patch.object(probe, "git", wraps=probe.git) as asked:
            with probe.quick():
                tops = {config.git_repo(folder) for _ in range(5)}
                config.project_root(folder)
            self.assertEqual(asked.call_count, 1)
            self.assertEqual(tops, {(folder, os.path.join(folder, ".git"))})
            config.git_repo(folder)
            config.git_repo(folder)
            self.assertEqual(asked.call_count, 3, "the MCP server must see a repository made later")

    def test_a_folder_that_is_no_repository_and_one_that_stalls_are_both_no_repository(self):
        from xsm import config, probe
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(config.git_repo(plain), (None, None))
        self.assertEqual(config.project_root(plain), os.path.realpath(plain))
        with mock.patch.object(probe.subprocess, "Popen", _StuckGit), probe.quick(0.2):
            self.assertEqual(config.git_repo("/stalled"), (None, None))

    def test_a_settings_file_in_a_folder_that_does_not_answer_tightens_nothing(self):
        from xsm import paths, probe, registry
        home = os.path.join(self.tmp, "claude-home")
        project = os.path.join(self.tmp, "project")
        paths.write_json(os.path.join(home, "settings.json"), {"crossSessionInbound": "accept"})
        paths.write_json(os.path.join(project, ".claude", "settings.json"),
                         {"crossSessionInbound": "refuse"})
        self.assertEqual(registry.inbound_setting(home, project), "refuse", "as it was")
        real = paths.read_json

        def stalled(path, *a, **k):
            if path.startswith(project):
                time.sleep(30)
            return real(path, *a, **k)

        with mock.patch.object(paths, "read_json", stalled), probe.quick(0.2):
            began = time.monotonic()
            self.assertEqual(registry.inbound_setting(home, project), "accept")
            self.assertEqual(registry.inbound_setting(home, project), "accept")
            self.assertLess(time.monotonic() - began, 1.5, "asked once, and not for long")

    def test_the_settings_of_a_folder_are_read_once_inside_quick(self):
        from xsm import paths, probe, registry
        home = os.path.join(self.tmp, "claude-home2")
        project = os.path.join(self.tmp, "project2")
        paths.write_json(os.path.join(project, ".claude", "settings.local.json"),
                         {"crossSessionInbound": "hold"})
        real = paths.read_json
        reads = []

        def counting(path, *a, **k):
            if path.startswith(project):
                reads.append(path)
            return real(path, *a, **k)

        with mock.patch.object(paths, "read_json", counting):
            with probe.quick():
                for _ in range(4):
                    self.assertEqual(registry.inbound_setting(home, project), "hold")
            self.assertEqual(len(reads), 2, "settings.json and settings.local.json, once")


class ListTest(TempState):
    """The command itself, with a folder whose git and files never answer."""

    def setUp(self):
        super().setUp()
        from xsm import registry, workers
        self.folders = []
        for n in range(4):
            folder = os.path.realpath(os.path.join(self.tmp, "work", "f%d" % n))
            os.makedirs(folder)
            subprocess.run(["git", "init", "-q", folder], check=True)
            self.folders.append(folder)
            registry.upsert("claude", os.path.join(self.tmp, "homes", "claude"), "s%d" % n,
                            os.getpid(), folder, name="n%d" % n)
        self.me = registry.by_session("claude", "s0")
        for patch in (mock.patch.object(registry, "me", return_value=self.me),
                      mock.patch.object(workers, "human_terminal", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def _list(self, *argv):
        from xsm import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["list"] + list(argv))
        return code, out.getvalue()

    def test_it_asks_git_once_per_folder(self):
        from xsm import probe
        with mock.patch.object(probe, "git", wraps=probe.git) as asked:
            code, text = self._list("-a")
        self.assertEqual(code, 0, text)
        self.assertLessEqual(asked.call_count, len(self.folders), "it was 42 for 8 folders")
        self.assertEqual(len([l for l in text.splitlines() if "@claude" in l]), 4, text)

    def test_a_stalled_folder_is_listed_and_costs_about_a_second_at_most(self):
        from xsm import paths, probe
        stalled = self.folders[2]
        real_popen, real_read = subprocess.Popen, paths.read_json

        def popen(args, *a, **k):
            return _StuckGit() if args[:2] == ["git", "-C"] and args[2] == stalled \
                else real_popen(args, *a, **k)

        def read(path, *a, **k):
            if path.startswith(stalled):
                time.sleep(30)
            return real_read(path, *a, **k)

        with mock.patch.object(probe.subprocess, "Popen", popen), \
                mock.patch.object(paths, "read_json", read), \
                mock.patch.object(probe, "QUICK_TIMEOUT", 0.3):
            for argv in (["-a"], []):
                began = time.monotonic()
                code, text = self._list(*argv)
                took = time.monotonic() - began
                self.assertEqual(code, 0, text)
                self.assertLess(took, 3.0, "a folder that does not answer must not hold it")
                if argv:
                    self.assertIn("n2@", text, "the session in it is still listed")
                    self.assertEqual(len([l for l in text.splitlines() if "@claude" in l]), 4, text)


if __name__ == "__main__":
    unittest.main()
