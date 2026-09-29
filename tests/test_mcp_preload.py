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


if __name__ == "__main__":
    unittest.main()
