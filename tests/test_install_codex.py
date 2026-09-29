"""Codex install regressions; all installation targets live in temporary homes."""
import importlib.util
import json
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
            link.write_text("abracadabra")
            self.assertEqual(install.install_cli(), "foreign")
            self.assertEqual(link.read_text(), "abracadabra")
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


class CodexPluginTest(unittest.TestCase):
    """The repository is also a Codex plugin (`codex plugin add xsm@xsm`).
    Codex 0.158 installed it into plugins/cache/xsm/xsm/<version>, ran its two
    hooks once trusted and connected its MCP server (measured 2026-09-29)."""

    def _json(self, *parts):
        return json.loads((REPO.joinpath(*parts)).read_text())

    def test_the_codex_manifests_point_at_files_that_exist(self):
        plugin = self._json(".codex-plugin", "plugin.json")
        self.assertEqual(plugin["name"], "xsm")
        self.assertEqual(plugin["version"], self._json(".claude-plugin", "plugin.json")["version"])
        for field in ("skills", "hooks", "mcpServers"):
            self.assertTrue((REPO / plugin[field]).exists(), field)
        market = self._json(".agents", "plugins", "marketplace.json")
        self.assertEqual([p["name"] for p in market["plugins"]], ["xsm"])
        self.assertEqual(market["plugins"][0]["source"]["path"], "./")

    def test_the_codex_hooks_are_the_events_a_direct_install_writes(self):
        from xsm import install
        hooks = self._json("hooks", "codex-hooks.json")["hooks"]
        self.assertEqual(sorted(hooks), sorted(install.CODEX_EVENTS))
        for event, groups in hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertIn("${PLUGIN_ROOT}", hook["command"], event)
        # Codex takes a stdio command only as a bare name or a contained ./ path.
        server = self._json("hooks", "codex-mcp.json")["mcpServers"]["xsm"]
        self.assertEqual(server["command"], "./hooks/xsm-mcp")
        self.assertTrue(os.access(REPO / "hooks/xsm-mcp", os.X_OK))

    def _codex_home(self, tmp, enabled=True):
        home = Path(tmp) / ".codex"
        root = home / "plugins/cache/xsm/xsm/0.4.6"
        (root / ".codex-plugin").mkdir(parents=True)
        (root / "hooks").mkdir()
        shutil.copy(REPO / "hooks/codex-hooks.json", root / "hooks")
        (home / "config.toml").write_text(
            '[plugins."xsm@xsm"]\n%s\n' % ("" if enabled else "enabled = false"))
        return home, root

    def test_a_codex_home_with_the_plugin_is_seen_and_not_installed_into(self):
        from xsm import install
        with tempfile.TemporaryDirectory() as tmp:
            home, root = self._codex_home(tmp)
            self.assertEqual(install.codex_plugin(str(home)),
                             {"key": "xsm@xsm", "version": "0.4.6", "root": str(root)})
            self.assertEqual(install.plugin_installed(str(home)), "0.4.6")
            (root.parent / "0.4.10/.codex-plugin").mkdir(parents=True)
            self.assertEqual(install.plugin_installed(str(home)), "0.4.10")
            shutil.rmtree(root.parent / "0.4.10")
            env = dict(os.environ, HOME=tmp, CODEX_HOME=str(home), XSM_HOME=str(Path(tmp) / ".xsm"),
                       CLAUDE_CONFIG_DIR=str(Path(tmp) / ".claude"), PYTHONPATH=str(REPO))
            result = subprocess.run(
                [sys.executable, "-m", "xsm", "install", "--codex-home", str(home), "--no-mcp",
                 "--python", sys.executable], cwd=tmp, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refused", result.stderr)
            self.assertFalse((home / "hooks.json").exists())
        with tempfile.TemporaryDirectory() as tmp:
            home, _ = self._codex_home(tmp, enabled=False)
            self.assertIsNone(install.codex_plugin(str(home)))

    def test_trust_is_read_for_the_plugin_hooks(self):
        from xsm import install
        with tempfile.TemporaryDirectory() as tmp:
            home, root = self._codex_home(tmp)
            self.assertEqual(install.codex_trust(str(home)),
                             {"SessionStart": False, "UserPromptSubmit": False})
            hooks = json.loads((root / "hooks/codex-hooks.json").read_text())["hooks"]
            lines = []
            for event, label in (("SessionStart", "session_start"),
                                 ("UserPromptSubmit", "user_prompt_submit")):
                group = hooks[event][0]
                lines.append('[hooks.state."xsm@xsm:hooks/codex-hooks.json:%s:0:0"]\n'
                             'trusted_hash = "%s"\n'
                             % (label, install.codex_hook_hash(event, group, group["hooks"][0])))
            with open(home / "config.toml", "a") as fh:
                fh.write("".join(lines))
            self.assertEqual(install.codex_trust(str(home)),
                             {"SessionStart": True, "UserPromptSubmit": True})

    def test_session_start_links_the_cli_only_from_a_plugin_copy(self):
        from xsm import install, receive
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, HOME=tmp):
            link = Path(tmp) / ".local/bin/xsm"
            receive._link_cli()
            self.assertFalse(link.exists(), "a checkout's hook leaves PATH to the person")
            copy = Path(tmp) / "plugins/cache/xsm/xsm/0.4.6"
            with mock.patch.object(install, "REPO", str(copy)):
                receive._link_cli()
            self.assertEqual(link.readlink(), copy / "bin/xsm")

    def test_a_link_to_a_newer_plugin_version_is_kept(self):
        from xsm import install
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, HOME=tmp):
            cache = Path(tmp) / "plugins/cache/xsm/xsm"
            for version in ("0.4.9", "0.4.10"):
                (cache / version / "xsm").mkdir(parents=True)
                (cache / version / "xsm/install.py").touch()
                (cache / version / "bin").mkdir()
                (cache / version / "bin/xsm").touch()
            link = Path(tmp) / ".local/bin/xsm"
            link.parent.mkdir(parents=True)
            link.symlink_to(cache / "0.4.10/bin/xsm")
            with mock.patch.object(install, "REPO", str(cache / "0.4.9")):
                self.assertEqual(install.install_cli(), "newer")
            self.assertEqual(link.readlink(), cache / "0.4.10/bin/xsm")
            link.unlink()
            link.symlink_to(cache / "0.4.9/bin/xsm")
            with mock.patch.object(install, "REPO", str(cache / "0.4.10")):
                self.assertEqual(install.install_cli(), "replaced")

    def test_the_config_is_read_in_the_spellings_toml_allows(self):
        from xsm import install
        for text, found in (('[plugins."xsm@xsm"]  # added by codex\nenabled = true\n', True),
                            ("[plugins.'xsm@xsm']\n", True),
                            ('[plugins."xsm@xsm"]\n"enabled" = false\n', False),
                            ('[plugins."other@xsm"]\n', False)):
            for no_tomllib in (False, True):     # 3.9 has no tomllib
                with tempfile.TemporaryDirectory() as tmp, \
                        mock.patch.dict(sys.modules, {"tomllib": None} if no_tomllib else {}):
                    home, _ = self._codex_home(tmp)
                    (home / "config.toml").write_text(text)
                    self.assertEqual(install.codex_plugin(str(home)) is not None, found,
                                     (text, no_tomllib))

    def _direct_install(self, home):
        from xsm import install
        (home / "hooks.json").write_text(json.dumps({"hooks": {
            event: [{"hooks": [{"type": "command", "command": install.hook_command("codex", event)}]}]
            for event in install.CODEX_EVENTS}}))
        with open(home / "config.toml", "a") as fh:
            fh.write('[mcp_servers.xsm]\ncommand = "python3"\n')

    def test_what_a_direct_install_left_beside_the_plugin_is_reported_and_cleared(self):
        from xsm import install
        with tempfile.TemporaryDirectory() as tmp:
            home, root = self._codex_home(tmp)
            self._direct_install(home)
            (home / "skills").mkdir()
            (home / "skills/xsm").symlink_to(REPO / "skills/xsm")
            found = install.leftovers(str(home))
            self.assertIn(str(home / "hooks.json"), found)
            self.assertIn(str(home / "skills/xsm"), found)
            self.assertTrue(any("mcp_servers" in f for f in found))
            # Trust follows the plugin's hooks, not the leftover groups.
            self.assertEqual(install.codex_trust(str(home)),
                             {"SessionStart": False, "UserPromptSubmit": False})
            with mock.patch.object(install, "remove_mcp", return_value=True) as removed:
                done = install.clear_codex_leftovers(str(home))
            removed.assert_called_once()
            self.assertEqual(done, ["hook groups", "MCP server", "skill link"])
            self.assertEqual(json.loads((home / "hooks.json").read_text())["hooks"], {})
            self.assertFalse(os.path.lexists(home / "skills/xsm"))

    def test_only_a_codex_session_start_links_the_cli(self):
        from xsm import receive
        for runtime, calls in (("codex", 1), ("claude", 0)):
            with mock.patch.object(receive, "detect_runtime", return_value=runtime), \
                    mock.patch.object(receive, "register", return_value=None), \
                    mock.patch.object(receive.housekeeping, "maybe_prune"), \
                    mock.patch.object(receive, "_link_cli") as link:
                receive._handle({"hook_event_name": "SessionStart", "session_id": "s"})
            self.assertEqual(link.call_count, calls, runtime)


if __name__ == "__main__":
    unittest.main()
