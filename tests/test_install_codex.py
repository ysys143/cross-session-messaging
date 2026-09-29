"""Codex install regressions; all installation targets live in temporary homes."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class CodexInstallTest(unittest.TestCase):
    def test_cli_link_preserves_foreign_files_and_replaces_xsm_links(self):
        from xsm import install
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, HOME=tmp):
            link = Path(tmp) / ".local/bin/xsm"
            self.assertEqual(install.install_cli(), "linked")
            self.assertEqual(install.install_cli(), "current")
            link.unlink()
            link.write_text("아브라카다브라")
            self.assertEqual(install.install_cli(), "foreign")
            self.assertEqual(link.read_text(), "아브라카다브라")
            link.unlink()
            foreign = Path(tmp) / "other-tool"
            link.symlink_to(foreign)
            self.assertEqual(install.install_cli(), "foreign")
            self.assertEqual(link.readlink(), foreign)
            # A person's own checkout stays linked; only a plugin version moves.
            checkout = Path(tmp) / "checkout"
            cached = Path(tmp) / "plugins/cache/xsm/xsm/0.4.4"
            for repo in (checkout, cached):
                (repo / "xsm").mkdir(parents=True)
                (repo / "xsm/install.py").touch()
                (repo / "bin").mkdir()
                (repo / "bin/xsm").touch()
            link.unlink()
            link.symlink_to(checkout / "bin/xsm")
            self.assertEqual(install.install_cli(), "foreign")
            self.assertEqual(link.readlink(), checkout / "bin/xsm")
            for old in (cached / "bin/xsm", Path(tmp) / "plugins/cache/xsm/xsm/0.4.5/bin/xsm"):
                link.unlink()
                link.symlink_to(old)
                self.assertEqual(install.install_cli(), "replaced")
                self.assertEqual(link.resolve(), REPO / "bin/xsm")
            for foreign in (Path(tmp) / "unrelated/xsm/xsm/custom/bin/xsm",
                            Path(tmp) / "plugins/cache/xsm/xsm/custom/bin/xsm"):
                link.unlink()
                link.symlink_to(foreign)
                if "unrelated" in foreign.parts:
                    self.assertEqual(install.install_cli(), "foreign")
                    self.assertEqual(link.readlink(), foreign)
                foreign.parent.mkdir(parents=True)
                foreign.write_text("another tool")
                self.assertEqual(install.install_cli(), "foreign")
                self.assertEqual(link.readlink(), foreign)
                self.assertEqual(foreign.read_text(), "another tool")

    def test_install_links_cli_into_local_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            env = dict(os.environ, HOME=tmp, CODEX_HOME=str(home),
                       CLAUDE_CONFIG_DIR=str(Path(tmp) / ".claude"),
                       XSM_HOME=str(Path(tmp) / ".xsm"), PYTHONPATH=str(REPO),
                       PATH=os.pathsep.join([str(Path(tmp) / ".local/bin"),
                                             "/usr/bin", "/bin"]))
            result = subprocess.run(
                [sys.executable, "-m", "xsm", "install", "--codex-home", str(home),
                 "--no-mcp", "--python", sys.executable],
                cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            link = Path(tmp) / ".local/bin/xsm"
            self.assertTrue(link.exists(), str(link))
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), REPO / "bin/xsm")
            self.assertEqual(subprocess.run(["xsm", "--help"], cwd=tmp, env=env, capture_output=True).returncode, 0)
            env["PATH"] = "/usr/bin:/bin"
            result = subprocess.run(result.args, cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("add ~/.local/bin to PATH", result.stdout)
            self.assertNotIn("ln -s", result.stdout)

    def test_refresh_relinks_skill_from_previous_plugin_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp).resolve() / "plugins/cache/xsm/xsm"
            old, new = cache / "0.4.5", cache / "0.4.6"
            for repo in (old, new):
                for part in ("skills/xsm", "xsm"):
                    shutil.copytree(REPO / part, repo / part)
            home = Path(tmp) / ".codex"
            link = home / "skills/xsm"
            link.parent.mkdir(parents=True)
            link.symlink_to(old / "skills/xsm")
            spec = importlib.util.spec_from_file_location("xsm.install", new / "xsm/install.py")
            install = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(install)
            self.assertEqual(install.skill_state(str(home))[0], "link-stale")
            self.assertEqual(install.install_skill(str(home))[0], "link-stale")
            self.assertEqual(link.resolve(), old / "skills/xsm")
            self.assertEqual(install.stale_copies(str(home), "codex"), [str(link)])
            install.install_skill(str(home), refresh=True)
            self.assertEqual(link.resolve(), new / "skills/xsm")
            link.unlink()
            link.symlink_to(old / "skills/xsm")
            shutil.rmtree(old)
            self.assertEqual(install.skill_state(str(home))[0], "link-stale")
            self.assertEqual(install.stale_copies(str(home), "codex"), [str(link)])
            install.install_skill(str(home), refresh=True)
            self.assertEqual(link.resolve(), new / "skills/xsm")
            link.unlink()
            foreign = Path(tmp) / "foreign"
            foreign.mkdir()
            (foreign / "SKILL.md").write_text("---\nname: other\n---\n")
            link.symlink_to(foreign)
            self.assertEqual(install.install_skill(str(home), refresh=True)[0], "foreign")
            self.assertEqual(link.readlink(), foreign)
            link.unlink()
            checkout = Path(tmp) / "checkout/skills/xsm"
            shutil.copytree(REPO / "skills/xsm", checkout)
            link.symlink_to(checkout)
            self.assertEqual(install.install_skill(str(home), refresh=True)[0], "foreign")
            self.assertEqual(link.readlink(), checkout)


if __name__ == "__main__":
    unittest.main()
