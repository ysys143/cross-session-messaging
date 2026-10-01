"""A session whose hooks never ran can still send, and be found (audit,
2026-10-01): the CLI and the MCP server register it from what the environment and
Claude's own record say, the plugin counts as consent, and a sender naming it is
told it is open but unregistered and how it gets registered."""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_xsm  # noqa: E402

# Module names only: a class imported here by name would run its tests twice.
TempState = test_xsm.TempState
REPO = test_xsm.REPO


class SelfRegistrationTest(TempState):
    """A3: a session whose hooks never ran can still send, and be found."""

    def _plugin_home(self, name="claude-plugin", disabled=False):
        from xsm import paths
        home = os.path.join(self.tmp, name)
        cache = os.path.join(home, "plugins", "cache", "xsm", "xsm", "0.4.15")
        os.makedirs(cache)
        paths.write_json(os.path.join(home, "plugins", "installed_plugins.json"),
                         {"version": 2, "plugins": {"xsm@xsm": [
                             {"version": "0.4.15", "installPath": cache}]}})
        if disabled:
            paths.write_json(os.path.join(home, "settings.json"),
                             {"enabledPlugins": {"xsm@xsm": False}})
        return os.path.realpath(home)

    def test_the_plugin_is_consent_a_disabled_one_is_not(self):
        from xsm import registry
        self.assertIsNone(registry.self_consent(self._plugin_home()))
        self.assertIn("xsm is not installed in", registry.self_consent(
            self._plugin_home("claude-plugin-off", disabled=True)))
        nothing = os.path.realpath(os.path.join(self.tmp, "claude-nothing"))
        os.makedirs(nothing)
        self.assertIn("xsm is not installed in", registry.self_consent(nothing))

    def _native(self, home, pid, sid="sess-1", cwd=None):
        """Claude's own record of a session, in <home>/sessions/<pid>.json."""
        from xsm import paths
        paths.write_json(os.path.join(home, "sessions", "%s.json" % pid),
                         {"pid": pid, "sessionId": sid, "cwd": cwd or self.tmp, "name": "late"})

    def test_a_cli_in_such_a_session_registers_it_from_its_environment(self):
        from xsm import registry
        home = self._plugin_home()
        self._native(home, os.getpid())
        env = {"CLAUDE_CONFIG_DIR": home, "CLAUDE_CODE_SESSION_ID": "sess-1"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(registry.identity, "socket_live", return_value=True):
            me = registry.me()
        self.assertEqual((me["session_id"], me["runtime"], me["pid"], me["adopted"]),
                         ("sess-1", "claude", os.getpid(), True))

    def test_without_claudes_own_record_the_socket_and_pid_of_the_shell_name_it(self):
        """No sessions/ file (another Claude version, a shell that cannot read the
        home): CLAUDE_CODE_MESSAGING_SOCKET is named after the pid."""
        from xsm import registry
        home = self._plugin_home()
        sock = "/tmp/cc-socks/%d.sock" % os.getpid()
        env = {"CLAUDE_CONFIG_DIR": home, "CLAUDE_CODE_SESSION_ID": "sess-2",
               "CLAUDE_CODE_MESSAGING_SOCKET": sock}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(registry.identity, "socket_live", return_value=True):
            me = registry.me()
        self.assertEqual((me["session_id"], me["pid"], me["socket"]), ("sess-2", os.getpid(), sock))
        env = {"CLAUDE_CONFIG_DIR": home, "CLAUDE_CODE_SESSION_ID": "sess-3",
               "CLAUDE_PID": str(os.getpid())}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(registry.me()["session_id"], "sess-3")

    def test_a_dead_socket_or_no_pid_is_not_a_session(self):
        from xsm import registry
        home = self._plugin_home()
        env = {"CLAUDE_CONFIG_DIR": home, "CLAUDE_CODE_SESSION_ID": "sess-4",
               "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/cc-socks/%d.sock" % os.getpid()}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(registry.identity, "socket_live", return_value=False):
            self.assertIsNone(registry.me())
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home,
                                          "CLAUDE_CODE_SESSION_ID": "sess-5"}):
            self.assertIsNone(registry.me(), "nothing says which process it is")

    def test_a_home_without_xsm_registers_nothing(self):
        from xsm import registry
        home = os.path.realpath(os.path.join(self.tmp, "claude-bare"))
        self._native(home, os.getpid())
        env = {"CLAUDE_CONFIG_DIR": home, "CLAUDE_CODE_SESSION_ID": "sess-1"}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(registry.me())
            self.assertIn("xsm is not installed", registry.unregistered_reason())

    def test_the_mcp_server_registers_its_parent_session_by_pid(self):
        from xsm import mcp, registry
        home = self._plugin_home()
        self._native(home, os.getpid(), "sess-mcp")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}), \
                mock.patch.object(mcp.identity, "ancestor_pid", return_value=os.getpid()):
            me = mcp.Server(io.StringIO(""), io.StringIO()).session()
        self.assertEqual((me["session_id"], me["runtime"]), ("sess-mcp", "claude"))
        # A Codex server whose environment carries a Claude session id is not that session.
        env = {"CLAUDE_CONFIG_DIR": home, "CLAUDE_CODE_SESSION_ID": "sess-mcp-env"}
        other = os.path.join(home, "sessions", "%d.json" % os.getpid())
        os.unlink(other)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(mcp.identity, "ancestor_pid", return_value=os.getpid()):
            self.assertIsNone(mcp.Server(io.StringIO(""), io.StringIO()).session())

    def test_a_codex_thread_that_has_not_registered_is_adopted_where_xsm_is_installed(self):
        from xsm import registry
        found = {"session_id": "t-1", "runtime": "codex"}
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "t-1"}), \
                mock.patch.object(registry, "adopt_codex_self", return_value=found) as adopt:
            rec, how = registry._me(None, None)
        self.assertEqual((rec, how), (found, "adopted"))
        adopt.assert_called_once_with()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "t-1"}), \
                mock.patch.object(registry, "adopt_codex_self", return_value=None):
            self.assertEqual(registry._me(None, None), (None, "none:codex-thread-unregistered"))

    def test_adopt_codex_self_tries_the_thread_then_the_process_table(self):
        from xsm import registry
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "t-2"}), \
                mock.patch.object(registry.identity, "ancestor_pid", return_value=77), \
                mock.patch.object(registry, "adopt_codex_thread", return_value=None) as thread, \
                mock.patch.object(registry, "adopt_open_codex") as table:
            self.assertIsNone(registry.adopt_codex_self())
        self.assertEqual(thread.call_args[0][:2], ("t-2", 77))
        table.assert_called_once_with()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
            self.assertIsNone(registry.adopt_codex_self())

    def test_an_open_claude_session_that_is_not_registered_is_named_with_how_it_gets_registered(self):
        from xsm import resolve
        home = self._plugin_home()
        self._native(home, os.getppid(), "sess-open")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}), \
                mock.patch.object(resolve.registry.identity, "socket_live", return_value=True):
            for target in ("late", "late@%s" % os.path.basename(home)):
                with self.subTest(target):
                    found = resolve.resolve(target)
                    self.assertEqual(found.status, "unregistered")
                    self.assertIn("late@", found.reason)
                    self.assertIn("is open but has not registered with xsm", found.reason)
                    self.assertIn("registers itself at its next prompt", found.reason)
                    self.assertIn("xsm who", found.reason)
            ref = found.candidates[0]["ref"]
            self.assertEqual(resolve.resolve("ref:%s" % ref).status, "unregistered")

    def test_the_send_says_so_instead_of_no_such_session(self):
        from xsm import registry, send
        home = self._plugin_home()
        self._native(home, os.getppid(), "sess-open")
        registry.upsert("claude", os.path.join(self.tmp, "homes", "x"), "s-me", os.getpid(),
                        self.tmp, name="me")
        me = registry.by_session("claude", "s-me")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}), \
                mock.patch.object(registry.identity, "socket_live", return_value=True):
            result = send.send("late", "hi", sender=me)
        self.assertEqual(result.status, "refused")
        self.assertIn("is open but has not registered with xsm", result.reason)
        self.assertIn("registers itself at its next prompt", result.reason)


if __name__ == "__main__":
    unittest.main()
