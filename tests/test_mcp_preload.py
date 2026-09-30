"""An open session's MCP server must survive its plugin folder being deleted.

Codex removes the old version folder on `codex plugin add`; three open Codex
sessions then ran 0.4.7 servers from a deleted folder and failed their first
xsm tool call, which imports modules on first use (2026-09-30)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Start from a copy, the way a server starts from a plugin version folder,
# delete the copy, then import what the tools import on first use.
PROBE = """
import shutil, sys
root = sys.argv[1]
sys.path.insert(0, root)
from xsm import mcp
if sys.argv[2] == "preload":
    mcp.preload()
shutil.rmtree(root)
from xsm import config, inbox, send, receive, doc, workers
print("ok")
"""


class PreloadTest(unittest.TestCase):
    def run_probe(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "0.4.7")
            shutil.copytree(REPO / "xsm", os.path.join(root, "xsm"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            env.pop("PYTHONPATH", None)
            return subprocess.run([sys.executable, "-c", PROBE, root, mode], cwd=tmp, env=env,
                                  capture_output=True, text=True)

    def test_a_preloaded_server_keeps_working_after_its_folder_is_deleted(self):
        out = self.run_probe("preload")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ok", out.stdout)

    def test_without_preload_the_deleted_folder_breaks_the_first_tool(self):
        """The control: this is the failure the open sessions hit."""
        out = self.run_probe("none")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("cannot import name 'inbox'", out.stderr)

    def test_main_preloads_before_serving(self):
        source = (REPO / "xsm" / "mcp.py").read_text(encoding="utf-8")
        main = source[source.index("def main()"):]
        self.assertLess(main.index("preload()"), main.index("Server().serve()"))


class RemovedVersionTest(unittest.TestCase):
    """Issue #6: servers already running from a removed folder cannot be saved
    by preload; they must say what happened and what to do instead."""

    def test_an_import_error_from_a_removed_folder_says_what_to_do(self):
        from unittest import mock
        sys.path.insert(0, str(REPO))
        from xsm import mcp
        err = ImportError("cannot import name 'workers' from 'xsm'")
        with mock.patch.object(mcp.os.path, "isdir", return_value=False):
            note = mcp.removed_version_note(err, "xsm_join")
        self.assertIn("plugin update removed", note)
        self.assertIn("xsm join <project>", note)
        self.assertIn("new session", note)
        self.assertIsNone(mcp.removed_version_note(err, "xsm_join"), "folder present: no note")
        self.assertIsNone(mcp.removed_version_note(ValueError("x"), "xsm_join"))

    def test_doctor_finds_servers_running_from_a_removed_folder(self):
        from unittest import mock
        sys.path.insert(0, str(REPO))
        from xsm import install
        with tempfile.TemporaryDirectory() as tmp:
            live = os.path.join(tmp, "0.4.9", "hooks", "xsm-mcp.py")
            os.makedirs(os.path.dirname(live))
            Path(live).touch()
            gone = os.path.join(tmp, "0.4.7", "hooks", "xsm-mcp.py")
            ps = "  101 /usr/bin/python3 %s\n  202 /usr/bin/python3 %s\n  303 bash\n" % (live, gone)
            with mock.patch.object(install.subprocess, "run",
                                   return_value=mock.Mock(stdout=ps)):
                self.assertEqual(install.orphaned_servers(),
                                 [(202, os.path.join(tmp, "0.4.7"))])


class OrphanNoteTest(unittest.TestCase):
    def test_a_preloading_server_is_only_old_an_earlier_one_is_broken(self):
        sys.path.insert(0, str(REPO))
        from xsm import cli
        old = cli._orphan_note(1, "/h/.codex/plugins/cache/xsm/xsm/0.4.7")
        self.assertIn("fail until that session restarts", old)
        kept = cli._orphan_note(2, "/h/.codex/plugins/cache/xsm/xsm/0.4.9")
        self.assertIn("keeps working", kept)
        self.assertIn("keeps working", cli._orphan_note(3, "/h/x/xsm/xsm/0.4.10"))


if __name__ == "__main__":
    unittest.main()
