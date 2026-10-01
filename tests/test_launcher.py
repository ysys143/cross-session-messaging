"""The launcher runs its own folder's code, whatever the current directory holds
(2026-10-02, the live test on both Macs).

`bin/xsm` ran `python -m xsm`, and `-m` puts the current directory first on sys.path:
run inside another xsm checkout (a worktree), that checkout's code ran instead of the
launcher's. What xsm starts itself the same way (a detached reaper, a worker's finish)
had the same hole."""
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_xsm  # noqa: E402

# Module names only: a class imported here by name would run its tests twice.
TempState = test_xsm.TempState
REPO = test_xsm.REPO
# The system python is 3.9 on a Mac: the launcher must work there, where -P does not exist.
INTERPRETERS = [sys.executable] + [p for p in ("/usr/bin/python3",) if os.path.exists(p)
                                   and os.path.realpath(p) != os.path.realpath(sys.executable)]


class LauncherTest(TempState):
    def setUp(self):
        super().setUp()
        self.decoy = os.path.join(self.tmp, "elsewhere")
        os.makedirs(os.path.join(self.decoy, "xsm"))
        open(os.path.join(self.decoy, "xsm", "__init__.py"), "w").close()
        with open(os.path.join(self.decoy, "xsm", "cli.py"), "w") as fh:
            fh.write("def main(argv=None):\n    print('DECOY xsm ran')\n    return 0\n")
        with open(os.path.join(self.decoy, "xsm", "__main__.py"), "w") as fh:
            fh.write("from .cli import main\nraise SystemExit(main())\n")
        self.env = dict(os.environ, XSM_HOME=os.path.join(self.tmp, "state"))
        self.env.pop("PYTHONPATH", None)

    def _run(self, argv, cwd, python, **env):
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60,
                              env=dict(self.env, XSM_PYTHON=python, **env))

    def test_the_old_way_ran_whatever_xsm_was_in_the_current_directory(self):
        """What this fixes, shown: `python -m xsm` with the launcher's PYTHONPATH."""
        out = self._run([sys.executable, "-m", "xsm"], self.decoy, sys.executable,
                        PYTHONPATH=REPO)
        self.assertIn("DECOY", out.stdout)

    def test_the_launcher_runs_its_own_package_from_a_folder_with_another(self):
        for python in INTERPRETERS:
            with self.subTest(python):
                out = self._run([os.path.join(REPO, "bin", "xsm"), "--version"], self.decoy, python)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertNotIn("DECOY", out.stdout)
                self.assertIn(os.path.join(REPO, "bin", "xsm"), out.stdout)

    def test_through_a_symlink_on_path_too_and_with_arguments_and_an_exit_status(self):
        link = os.path.join(self.tmp, "bin-on-path", "xsm")
        os.makedirs(os.path.dirname(link))
        os.symlink(os.path.join(REPO, "bin", "xsm"), link)
        out = self._run([link, "who", "--bogus-flag"], self.decoy, sys.executable)
        self.assertNotIn("DECOY", out.stdout)
        self.assertNotEqual(out.returncode, 0, "the command's own status comes through")
        self.assertIn("xsm", out.stderr.splitlines()[0], "and it is argparse's xsm, not `-c`")
        self.assertNotIn("usage: -c", out.stderr)

    def test_the_current_directory_is_not_searched_for_anything_else_either(self):
        for python in INTERPRETERS:
            with self.subTest(python):
                out = self._run([os.path.join(REPO, "bin", "xsm"), "--version"], REPO, python)
                self.assertEqual(out.returncode, 0, out.stderr)

    def test_what_xsm_starts_itself_runs_the_same_way(self):
        from xsm import install
        out = subprocess.run(install.cli_argv("--version"), cwd=self.decoy, capture_output=True,
                             text=True, timeout=60, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("DECOY", out.stdout)
        argv = install.cli_argv("reap", "--after-pid", "7")
        self.assertEqual((argv[1], argv[3], argv[4:]), ("-c", install.REPO,
                                                        ["reap", "--after-pid", "7"]))

    def test_the_launcher_and_the_installer_carry_the_same_text(self):
        from xsm import install
        with open(os.path.join(REPO, "bin", "xsm"), encoding="utf-8") as fh:
            text = fh.read()
        found = re.search(r"^boot='(.*)'$", text, re.M)
        self.assertIsNotNone(found)
        self.assertEqual(found.group(1), install.BOOT)
        self.assertNotRegex(install.BOOT, r"['$`\\]", "sh keeps it whole")

    def test_no_launcher_uses_python_dash_m(self):
        for rel in ("bin/xsm", "hooks/xsm-hook", "hooks/xsm-mcp"):
            with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                code = [l for l in fh.read().splitlines() if not l.lstrip().startswith("#")]
            self.assertFalse([l for l in code if re.search(r"\s-m\s+xsm\b", l)], rel)
        with open(os.path.join(REPO, "xsm", "workers.py"), encoding="utf-8") as fh:
            self.assertNotRegex(fh.read(), r'"-m",\s*"xsm"')


if __name__ == "__main__":
    unittest.main()
