"""Workers: framework deference, approvals that only a person can grant, and
where a worker runs. Live behaviour (spawning real sessions) is covered by
TESTPLAN chapter 10; these tests pin the rules around it."""
import contextlib
import io
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_xsm import TempState  # noqa: E402


class FrameworkTest(TempState):
    """Inside Orca or herdr, starting and stopping workers belongs to them."""

    def test_detects_pane_markers(self):
        from xsm import workers
        self.assertEqual(workers.framework_host({"ORCA_TERMINAL_HANDLE": "t"}), "orca")
        self.assertEqual(workers.framework_host({"HERDR_PANE_ID": "p"}), "herdr")
        self.assertIsNone(workers.framework_host({"ORCA_USER_DATA_PATH": "/x"}),
                          "an app-level variable alone is not a pane the framework owns")

    def test_spawn_is_refused_inside_a_framework(self):
        from unittest import mock
        from xsm import workers
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "HERDR_"))}
        env["HERDR_PANE_ID"] = "p1"
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(workers.WorkerError) as cm:
                workers.spawn("claude")
        self.assertIn("herdr", str(cm.exception))

    def test_a_person_can_let_xsm_start_workers_inside_a_framework(self):
        """2026-09-23: a Codex session inside Orca is to orchestrate xsm
        workers itself, so the refusal can be lifted, per framework."""
        from unittest import mock
        from xsm import config, workers
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "HERDR_"))}
        env["ORCA_TERMINAL_HANDLE"] = "t1"
        config.set_framework_ignored("orca", True)
        with mock.patch.dict(os.environ, env, clear=True):
            workers.refuse_inside_framework()           # no longer refused
            env["ORCA_TERMINAL_HANDLE"], env["HERDR_PANE_ID"] = "", "p1"
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(workers.WorkerError, msg="only orca was lifted"):
                workers.refuse_inside_framework()
        config.set_framework_ignored("all", True)
        with mock.patch.dict(os.environ, env, clear=True):
            workers.refuse_inside_framework()
        config.set_framework_ignored("all", False)
        self.assertEqual(config.ignored_frameworks(), set(), "respect all restores every one")

    def test_only_a_person_lifts_the_refusal(self):
        from xsm import cli, config
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["frameworks", "ignore", "orca"])     # a test has no terminal
        self.assertNotEqual(code, 0)
        self.assertEqual(config.ignored_frameworks(), set())
        config.set_framework_ignored("orca", True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["frameworks", "respect", "orca"]), 0,
                             "restoring narrows, so anyone may")
        self.assertEqual(config.ignored_frameworks(), set())

    def test_stale_tmux_variables_are_not_a_pane(self):
        from xsm import workers
        self.assertIsNone(workers.tmux_pane({"TMUX": "/nonexistent,1,0", "TMUX_PANE": "%999999"}))


class ApprovalTest(TempState):
    def _worker(self, **extra):
        from xsm import workers
        rec = {"name": "w1", "runtime": "claude", "mode": "background", "approval_timeout": 5,
               "created": time.time(), "session_id": "s-w1"}
        rec.update(extra)
        workers.save(rec)
        return rec

    def test_silent_for_ordinary_sessions(self):
        from xsm import workers
        os.environ.pop("XSM_WORKER", None)
        self.assertIsNone(workers.permission_request({"tool_name": "Bash"}, "claude"))

    def test_waits_for_an_answer_and_relays_it(self):
        from xsm import workers
        self._worker()
        os.environ["XSM_WORKER"] = "w1"
        try:
            def answer_soon():
                for _ in range(50):
                    rows = workers.approvals()
                    if rows:
                        workers.answer(rows[0]["id"], False, "not today")
                        return
                    time.sleep(0.1)
            threading.Thread(target=answer_soon).start()
            out = workers.permission_request(
                {"tool_name": "Bash", "tool_input": {"command": "rm x"}}, "claude")
        finally:
            del os.environ["XSM_WORKER"]
        decision = out["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("not today", decision["message"])

    def test_times_out_to_deny(self):
        from xsm import workers
        self._worker(approval_timeout=1)
        os.environ["XSM_WORKER"] = "w1"
        try:
            out = workers.permission_request({"tool_name": "Bash", "tool_input": {}}, "claude")
        finally:
            del os.environ["XSM_WORKER"]
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_pane_workers_answer_in_their_pane(self):
        from xsm import workers
        self._worker(mode="pane")
        os.environ["XSM_WORKER"] = "w1"
        try:
            self.assertIsNone(workers.permission_request({"tool_name": "Bash"}, "codex"))
        finally:
            del os.environ["XSM_WORKER"]

    def test_agent_environment_is_not_a_person(self):
        from unittest import mock
        from xsm import workers
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            self.assertFalse(workers.human_terminal())

    def test_approvals_of_a_gone_worker_are_closed(self):
        from xsm import paths, workers
        os.makedirs(paths.path(workers.APPROVALS), exist_ok=True)
        paths.write_json(paths.path(workers.APPROVALS, "r2.json"),
                         {"id": "r2", "worker": "nobody", "status": "pending", "summary": "x"})
        self.assertEqual(workers.approvals(), [])
        self.assertEqual(paths.read_json(paths.path(workers.APPROVALS, "r2.json"))["status"],
                         "denied")

    def test_approving_needs_a_terminal(self):
        from xsm import paths, workers
        os.makedirs(paths.path(workers.APPROVALS), exist_ok=True)
        paths.write_json(paths.path(workers.APPROVALS, "r1.json"),
                         {"id": "r1", "worker": "w1", "status": "pending", "summary": "Bash: x"})
        workers.human_terminal = lambda: False          # what an agent's shell looks like
        with self.assertRaises(workers.WorkerError):
            workers.answer("r1", True)
        self.assertEqual(workers.answer("r1", False)["status"], "denied",
                         "denying is always allowed: it only narrows")

    def test_hook_error_never_answers_a_permission_request(self):
        import io
        from xsm import receive
        os.environ["XSM_FORCE_ERROR"] = "1"
        sys.stdin, saved = io.StringIO(json.dumps({
            "hook_event_name": "PermissionRequest", "tool_name": "Bash",
            "tool_input": {"command": "echo '[xsm v1 id=x]'"}})), sys.stdin
        out, sys.stdout = sys.stdout, io.StringIO()
        try:
            receive.main()
            printed = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = saved, out
            del os.environ["XSM_FORCE_ERROR"]
        self.assertEqual(printed, "")


class OnceTest(TempState):
    def test_once_worker_stops_when_its_answer_arrives(self):
        from xsm import workers
        workers.save({"name": "o1", "runtime": "claude", "mode": "background", "ref": "wwwwww",
                      "parent_ref": "pppppp", "once": True, "task_id": "m9", "created": 0})
        from unittest import mock
        stopped = []
        with mock.patch.object(workers.subprocess, "Popen",
                               lambda argv, **kw: stopped.append(argv[-1])):
            workers.on_reply("wwwwww", "m8", {"ref": "pppppp"})
            workers.on_reply("wwwwww", "m9", {"ref": "other"})
            self.assertEqual(stopped, [], "another task's answer, or another receiver, is not it")
            workers.on_reply("wwwwww", "m9", {"ref": "pppppp"})
        self.assertEqual(stopped, ["o1"])

    def test_once_without_a_task_id_is_never_stopped_by_a_reply(self):
        from xsm import workers
        workers.save({"name": "o2", "runtime": "claude", "mode": "background", "ref": "wwwwww",
                      "parent_ref": "pppppp", "once": True, "created": 0})
        from unittest import mock
        stopped = []
        with mock.patch.object(workers.subprocess, "Popen",
                               lambda argv, **kw: stopped.append(argv[-1])):
            workers.on_reply("wwwwww", None, {"ref": "pppppp"})
            workers.on_reply("wwwwww", "anything", {"ref": "pppppp"})
        self.assertEqual(stopped, [])


class CodexInstallTest(TempState):
    def test_an_earlier_permission_request_group_is_removed(self):
        from xsm import install, paths
        home = os.path.join(self.tmp, "codex-h")
        os.makedirs(home)
        paths.write_json(os.path.join(home, "hooks.json"), {"hooks": {"PermissionRequest": [
            {"hooks": [{"type": "command", "command": "other-tool"}]},
            {"hooks": [{"type": "command", "command": "py hook.py " + install.MARKER}]}]}},
            mode=0o644)
        install.apply(home, "codex")
        data = paths.read_json(os.path.join(home, "hooks.json"))
        self.assertEqual([g["hooks"][0]["command"] for g in data["hooks"]["PermissionRequest"]],
                         ["other-tool"], "only our group goes")
        self.assertTrue(install.apply(home, "codex").get("unchanged"))


FAKE_APP_SERVER = r"""#!/usr/bin/env python3
import json, os, sys
log = open(os.environ["FAKE_LOG"], "a")
def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    log.write(json.dumps(msg) + "\n"); log.flush()
    m = msg.get("method")
    if m == "initialize":
        out({"id": msg["id"], "result": {}})
    elif m in ("thread/start", "thread/resume"):
        out({"id": msg["id"], "result": {"thread": {"id": "t-new"}}})
    elif m == "turn/start":
        out({"id": msg["id"], "result": {}})
        out({"id": "srv-1", "method": "item/commandExecution/requestApproval",
             "params": {"command": "touch /outside", "reason": "outside the workspace"}})
    elif "result" in msg and msg.get("id") == "srv-1":
        text = "done" if msg["result"]["decision"] == "accept" else "declined"
        out({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": text}}})
        out({"method": "turn/completed", "params": {}})
"""


class FreshTuiAdoptionTest(TempState):
    """A Codex TUI that has not written its thread yet must not be matched to an
    older thread in the same folder (measured: a pane worker was registered as
    the earlier worker's thread and its task went nowhere)."""

    def test_older_thread_in_the_same_folder_is_not_adopted(self):
        from xsm import registry
        now = time.time()
        registry._codex_recent_threads = lambda home, within=0: [
            ("old", "cw1", self.tmp, "/r", now - 600, now - 60)]
        registry._running_codex = lambda: [(os.path.realpath(self.tmp), now - 10, None, 4242)]
        self.assertEqual(registry._open_codex_threads(self.tmp), [])

    def test_resume_from_the_picker_still_matches_an_older_thread(self):
        from xsm import registry
        now = time.time()
        registry._codex_recent_threads = lambda home, within=0: [
            ("old", "cw1", self.tmp, "/r", now - 600, now - 60)]
        registry._running_codex = lambda: [(os.path.realpath(self.tmp), now - 10, "?", 4242)]
        self.assertEqual([t[0] for t in registry._open_codex_threads(self.tmp)], ["old"])


class ResumeInsideTuiTest(TempState):
    def test_thread_touched_after_start_counts_when_nothing_newer(self):
        from xsm import registry
        now = time.time()
        registry._codex_recent_threads = lambda home, within=0: [
            ("resumed", "r", self.tmp, "/r", now - 900, now - 1)]
        registry._running_codex = lambda: [(os.path.realpath(self.tmp), now - 30, None, 4242)]
        self.assertEqual([t[0] for t in registry._open_codex_threads(self.tmp)], ["resumed"])


class DepthTest(TempState):
    """max_depth: worker levels below a top-level session. Default 1."""

    def _env(self, **extra):
        from unittest import mock
        env = {k: v for k, v in os.environ.items()
               if k not in ("XSM_WORKER", "XSM_MAX_DEPTH")}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_default_lets_a_session_spawn_and_stops_its_workers(self):
        from xsm import workers
        with self._env():
            self.assertEqual(workers.depth_budget(None), (1, 1))
        workers.save({"name": "w1", "depth": 1, "max_depth": 1, "created": 0})
        with self._env(XSM_WORKER="w1"):
            with self.assertRaises(workers.WorkerError) as cm:
                workers.depth_budget(None)
        self.assertIn("depth 2", str(cm.exception))

    def test_global_setting_and_spawn_option(self):
        from xsm import config, paths, workers
        paths.write_json(paths.path(config.CONFIG), {"max_depth": 2}, mode=0o644)
        with self._env():
            self.assertEqual(workers.depth_budget(None), (1, 2))
            self.assertEqual(workers.depth_budget(None, 3), (1, 3), "spawn can set it")
        with self._env(XSM_MAX_DEPTH="0"):
            with self.assertRaises(workers.WorkerError):
                workers.depth_budget(None)

    def test_a_worker_inherits_its_budget_and_can_only_narrow_it(self):
        from xsm import workers
        workers.save({"name": "w1", "depth": 1, "max_depth": 3, "created": 0})
        with self._env(XSM_WORKER="w1", XSM_MAX_DEPTH="1"):
            self.assertEqual(workers.depth_budget(None), (2, 3),
                             "the record decides, not an environment variable")
            self.assertEqual(workers.depth_budget(None, 2), (2, 2))
            with self.assertRaises(workers.WorkerError):
                workers.depth_budget(None, 5)

    def test_worker_found_by_its_session_when_the_variable_is_gone(self):
        from xsm import workers
        workers.save({"name": "w1", "depth": 1, "max_depth": 1, "session_id": "s-w1",
                      "created": 0})
        with self._env():
            with self.assertRaises(workers.WorkerError):
                workers.depth_budget({"session_id": "s-w1"})


class SafetyTest(TempState):
    def _env(self, **extra):
        from unittest import mock
        env = {k: v for k, v in os.environ.items()
               if k not in ("XSM_WORKER", "XSM_MAX_WORKERS", "XSM_MAX_DEPTH")}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_running_workers_per_session_are_capped(self):
        from xsm import workers
        workers.reap = lambda: []
        workers.state = lambda w: "running"
        for i in range(2):
            workers.save({"name": "w%d" % i, "parent_ref": "pppppp", "created": i})
        workers.save({"name": "other", "parent_ref": "qqqqqq", "created": 9})
        with self._env(XSM_MAX_WORKERS="2"):
            with self.assertRaises(workers.WorkerError) as cm:
                workers.check_concurrency({"ref": "pppppp"})
            workers.check_concurrency({"ref": "qqqqqq"})       # counted per session
        self.assertIn("limit is 2", str(cm.exception))

    def test_orphans_are_workers_of_sessions_that_are_over(self):
        from xsm import registry, workers
        registry.records = lambda: [{"ref": "live01", "state": "live"},
                                    {"ref": "ended1", "state": "ended"},
                                    {"ref": "stale1", "state": "stale"},
                                    {"ref": "unkn01", "state": "unknown"}]
        workers.state = lambda w: "running"
        for name, parent in (("a", "live01"), ("b", "ended1"), ("c", "stale1"),
                             ("d", "unkn01"), ("e", "gone01")):
            workers.save({"name": name, "parent_ref": parent, "created": time.time()})
        self.assertEqual(sorted(w["name"] for w, _ in workers.orphans()), ["b", "c", "e"],
                         "an unknown parent is not over")

    def test_a_parent_goodbye_reaps_once_its_process_is_gone(self):
        from unittest import mock
        from xsm import receive, registry, workers
        rec = registry.upsert("claude", self.tmp, "s1", os.getpid(), self.tmp)
        workers.save({"name": "b", "parent_ref": rec["ref"], "created": 0})
        started = []
        with mock.patch.object(workers.subprocess, "Popen",
                               lambda argv, **kw: started.append(argv[3:])):
            receive.handle({"hook_event_name": "SessionEnd", "session_id": "s1",
                            "session_title": "boss", "reason": "exit"})
        self.assertEqual(started, [["reap", "--after-pid", str(os.getpid())]],
                         "the parent is still alive during its own SessionEnd")

    def test_claude_workers_never_start_in_the_users_own_mode(self):
        from xsm import workers
        modes = {}
        for mode in ("pane", "background"):
            argv = workers._claude_argv({"name": "w", "mode": mode}, "/s.json")
            modes[mode] = argv[argv.index("--permission-mode") + 1]
        self.assertEqual(modes, {"pane": "default", "background": "acceptEdits"},
                         "never the user's own mode; a background worker edits its folder unasked")

    def test_a_background_claude_worker_is_sandboxed_like_codex(self):
        from xsm import paths, workers
        w = {"name": "sb", "mode": "background", "approval_timeout": 5}
        os.makedirs(os.path.join(self.tmp, "workers", "sb"))
        settings = json.load(open(workers._claude_worker_settings(w)))
        self.assertEqual(settings["sandbox"]["enabled"], True)
        self.assertTrue(settings["sandbox"]["autoAllowBashIfSandboxed"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
        self.assertEqual(settings["sandbox"]["filesystem"]["allowWrite"], [paths.HOME],
                         "xsm stays sandboxed; its store is the one place it may write outside")
        self.assertEqual(settings["permissions"]["allow"][:5],
                         ["Bash", "Monitor", "Read", "Glob", "Grep"],
                         "reads anywhere, like Codex's workspace-write; writes stay in the folder")
        self.assertEqual(settings["env"], {"XSM_SANDBOXED": "1"},
                         "so `xsm send` to a Codex peer says 'use MCP' instead of failing")
        self.assertIn("mcp__xsm__xsm_inbox", settings["permissions"]["allow"],
                      "reading messages must not wait on a person (S10 collab run 5)")
        self.assertNotIn("Write", settings["permissions"]["allow"])
        self.assertNotIn("Edit", settings["permissions"]["allow"])
        self.assertIn("mcp__xsm__xsm_send", settings["permissions"]["allow"],
                      "the route to a Codex peer runs outside the sandbox")
        from xsm import config
        codex_home = os.path.realpath(os.path.join(self.tmp, "codex-home"))
        os.makedirs(codex_home)
        open(os.path.join(codex_home, "state_5.sqlite"), "w").close()
        open(os.path.join(codex_home, "config.toml"), "w").close()
        config.add_home(codex_home, "codex")
        again = json.load(open(workers._claude_worker_settings(w)))
        self.assertIn(os.path.join(codex_home, "state_5.sqlite"),
                      again["sandbox"]["filesystem"]["allowWrite"], "codex queue opens its state DB")
        self.assertNotIn(os.path.join(codex_home, "config.toml"),
                         again["sandbox"]["filesystem"]["allowWrite"])
        self.assertIn(os.path.join(codex_home, "ipc"), again["sandbox"]["network"]["allowUnixSockets"],
                      "codex queue starts an embedded app server")
        self.assertTrue(again["sandbox"]["network"]["allowLocalBinding"])
        self.assertIn(workers.CLAUDE_SOCKETS, settings["sandbox"]["network"]["allowUnixSockets"],
                      "reporting back goes through a peer's inbox socket")
        self.assertIn("PermissionRequest", settings["hooks"], "past the sandbox, a person decides")
        pane = {"name": "pn", "mode": "pane", "approval_timeout": 5}
        os.makedirs(os.path.join(self.tmp, "workers", "pn"))
        self.assertNotIn("sandbox", json.load(open(workers._claude_worker_settings(pane))))


class GrantTest(TempState):
    """Dangerous worker options need the user's explicit permission, given
    through the xsm_grant MCP tool, unless a person types the spawn."""
    ME = {"ref": "pppppp", "name": "boss", "alias": "claude-4", "runtime": "claude"}

    def test_a_grant_covers_one_matching_spawn(self):
        from xsm import workers
        g = workers.create_grant("pppppp", "codex", self.tmp, ["full_access"], "allow once")
        workers.use_grant(g["id"], self.ME, "codex", self.tmp, ["full_access"])
        with self.assertRaises(workers.WorkerError):
            workers.use_grant(g["id"], self.ME, "codex", self.tmp, ["full_access"])

    def test_a_grant_is_bound_to_its_session_runtime_folder_and_options(self):
        from xsm import workers
        cases = [({"ref": "other1"}, "codex", self.tmp, ["full_access"], "another session"),
                 (self.ME, "claude", self.tmp, ["full_access"], "it is for codex"),
                 (self.ME, "codex", "/", ["full_access"], "it is for"),
                 (self.ME, "codex", self.tmp, ["full_access", "trust_hooks"], "does not cover")]
        for caller, runtime, cwd, options, why in cases:
            g = workers.create_grant("pppppp", "codex", self.tmp, ["full_access"], "allow once")
            with self.assertRaises(workers.WorkerError) as cm:
                workers.use_grant(g["id"], caller, runtime, cwd, options)
            self.assertIn(why, str(cm.exception))

    def test_an_expired_grant_is_refused(self):
        from xsm import paths, workers
        g = workers.create_grant("pppppp", "codex", self.tmp, ["full_access"], "allow once")
        g["expires"] = 0
        paths.write_json(paths.path(workers.GRANTS, g["id"] + ".json"), g)
        with self.assertRaises(workers.WorkerError) as cm:
            workers.use_grant(g["id"], self.ME, "codex", self.tmp, ["full_access"])
        self.assertIn("expired", str(cm.exception))

    def test_an_agent_spawn_without_a_grant_is_refused(self):
        from unittest import mock
        from xsm import workers
        workers.human_terminal = lambda: False
        workers._check_installed = lambda home, runtime: None
        workers.check_concurrency = lambda caller: None
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "HERDR_", "TMUX"))}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(workers.WorkerError) as cm:
                workers.spawn("claude", cwd=self.tmp, full_access=True, caller=self.ME)
        self.assertIn("xsm_grant", str(cm.exception))

    def test_trust_hooks_is_for_codex_only(self):
        from unittest import mock
        from xsm import workers
        workers._check_installed = lambda home, runtime: None
        workers.check_concurrency = lambda caller: None
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "HERDR_", "TMUX"))}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(workers.WorkerError) as cm:
                workers.spawn("claude", cwd=self.tmp, trust_hooks=True, caller=self.ME)
        self.assertIn("Codex option", str(cm.exception))

    def test_mcp_grant_asks_and_records(self):
        import io
        from xsm import channel, mcp, workers
        here = os.path.join(self.tmp, "proj")
        os.makedirs(here)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"capabilities": {"elicitation": {}}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "xsm_grant", "arguments": {"runtime": "codex",
                                                       "options": ["full_access"],
                                                       "reason": "needs network"}}},
                {"jsonrpc": "2.0", "id": "xsm-1",
                 "result": {"action": "accept", "content": {"answer": "allow once"}}}]
        out = io.StringIO()
        server = mcp.Server(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        server.session = lambda: dict(self.ME, cwd=here)
        server.serve()
        replies = [json.loads(l) for l in out.getvalue().splitlines()]
        ask = next(m for m in replies if m.get("method") == "elicitation/create")
        self.assertIn("FULL ACCESS", ask["params"]["message"])
        text = next(m for m in replies if m.get("id") == 2)["result"]["content"][0]["text"]
        gid = text.split()[1].rstrip(":")
        workers.use_grant(gid, self.ME, "codex", here, ["full_access"])
        rec = channel.read(channel.resolve(here)[1])[-1]
        self.assertEqual((rec["tag"], rec["author"]["via"]), ("decision", "mcp-elicitation"))


class DangerousFlagsTest(TempState):
    def test_pane_codex_flags(self):
        import shlex
        from unittest import mock
        from xsm import workers
        seen = []
        def run(argv, **kw):
            seen.append(argv)
            return mock.Mock(returncode=0, stdout="%9 4242\n", stderr="")
        w = {"name": "p", "runtime": "codex", "home": self.tmp, "cwd": self.tmp, "mode": "pane",
             "model": "m", "full_access": True, "trust_hooks": True}
        with mock.patch.object(workers.subprocess, "run", run), \
                mock.patch.object(workers.time, "sleep", lambda s: None), \
                mock.patch.object(workers, "_tmux_type", lambda *a: None):
            workers._start_in_tmux(w, "%1")
        launched = next(a for a in seen if a[0] == "tmux")
        self.assertEqual(launched[:2], ["tmux", "split-window"], "the version check runs first")
        seen = [launched]
        command = shlex.split(seen[0][-1])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--dangerously-bypass-hook-trust", command)
        self.assertNotIn("on-request", command)

    def test_a_background_worker_is_a_real_tui_in_the_xsm_workers_session(self):
        """Never `claude -p` or `codex exec`: those run one prompt and are gone,
        so there is no session to send a message to (user decision, 2026-09-22)."""
        import shlex
        from unittest import mock
        from xsm import workers
        for runtime, sessions_exist in (("claude", False), ("codex", True)):
            seen = []
            def run(argv, **kw):
                seen.append(argv)
                if argv[1] == "has-session":
                    return mock.Mock(returncode=0 if sessions_exist else 1)
                return mock.Mock(returncode=0, stdout="%9 4242\n", stderr="")
            w = {"name": "b", "runtime": runtime, "home": self.tmp, "cwd": self.tmp,
                 "mode": "background", "model": "m", "approval_timeout": 5}
            os.makedirs(os.path.join(self.tmp, "workers", "b"), exist_ok=True)
            with mock.patch.object(workers.subprocess, "run", run), \
                    mock.patch.object(workers.time, "sleep", lambda s: None), \
                    mock.patch.object(workers, "_tmux_type", lambda *a: None):
                workers._start_in_tmux(w, None)
            launch = next(a for a in seen if a[0] == "tmux" and a[1] in ("new-window",
                                                                           "new-session"))
            self.assertEqual(launch[1], "new-window" if sessions_exist else "new-session")
            self.assertIn(workers.BACKGROUND_SESSION, launch)
            command = shlex.split(launch[-1])
            self.assertNotIn("-p", command)
            # The runtime is an absolute path now: xsm picks a codex that runs.
            at = next(i for i, word in enumerate(command) if os.path.basename(word) == runtime)
            self.assertNotIn("exec", command[at:])
            if runtime == "codex":
                self.assertEqual(command[command.index("-a") + 1], "never",
                                 "nobody to ask, and Codex has no hook to relay the question")
            self.assertEqual(w["pane"], "%9")

    def test_a_worker_in_the_default_claude_home_does_not_name_it(self):
        """Naming ~/.claude in CLAUDE_CONFIG_DIR makes Claude read folder trust
        from ~/.claude/.claude.json, not ~/.claude.json: every worker then sat
        at a trust prompt for a folder its caller already trusted."""
        from unittest import mock
        from xsm import workers
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/somewhere/else"}):
            default = workers._env({"name": "d", "runtime": "claude",
                                    "home": os.path.expanduser("~/.claude")})
            other = workers._env({"name": "o", "runtime": "claude", "home": "/x/.claude-2"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", default)
        self.assertEqual(other["CLAUDE_CONFIG_DIR"], "/x/.claude-2")

    def test_a_trust_screen_becomes_a_question_for_a_person(self):
        """The person is asked through xsm; they are not sent to a tmux screen."""
        from unittest import mock
        from xsm import paths, workers
        workers._check_installed = lambda home, runtime: None
        workers.check_concurrency = lambda caller: None
        started = []
        def start(worker, pane):
            worker.update({"pane": "%7", "pid": os.getpid(), "lstart": None})
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "HERDR_", "TMUX"))}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(workers, "_start_in_tmux", start), \
                mock.patch.object(workers, "_screen", lambda pane: "  Yes, I trust this folder"), \
                mock.patch.object(workers, "_finish_detached", lambda w: started.append(w["name"])), \
                mock.patch.object(workers.shutil, "which", lambda name: "/usr/bin/" + name):
            w = workers.spawn("claude", name="tw", cwd=self.tmp, background=True,
                              task={"id": "t1", "text": "do it"})
        req = paths.read_json(workers._approval_path(w["waiting"])) or {}
        self.assertEqual((req.get("status"), req.get("tool"), req.get("worker")),
                         ("pending", "folder-trust", "tw"))
        self.assertIn(self.tmp, req.get("summary", ""))
        self.assertEqual(started, ["tw"], "a detached finisher takes over; spawn returns at once")
        self.assertEqual((workers.load("tw") or {}).get("pending_task"), {"id": "t1", "text": "do it"})

    def test_the_finisher_answers_the_screen_the_way_the_person_did(self):
        from unittest import mock
        from xsm import paths, workers
        for answer in ("approved", "denied"):
            name = "f-" + answer
            workers.save({"name": name, "runtime": "claude", "mode": "background", "pane": "%7",
                          "cwd": self.tmp, "created": 0, "parent_ref": "pppppp"})
            w = workers.load(name) or {}
            req = workers._ask_trust(w)
            workers.save(w)
            req["status"] = answer
            paths.write_json(workers._approval_path(req["id"]), req)
            pressed, stopped = [], []
            def register(worker, wait):
                worker.update({"session_id": "s", "ref": "rrrrrr"})
            with mock.patch.object(workers, "_press", lambda wk, a: pressed.append(a)), \
                    mock.patch.object(workers, "_wait_for_registration", register), \
                    mock.patch.object(workers, "stop", lambda n, reason="": stopped.append(n)):
                workers.finish(name)
            if answer == "approved":
                self.assertEqual((pressed, stopped), (["yes"], []))
                after = workers.load(name) or {}
                self.assertNotIn("waiting", after)
                self.assertEqual(after.get("ref"), "rrrrrr")
            else:
                self.assertEqual((pressed, stopped), (["no"], [name]))

    def test_codex_is_not_named_over_a_trust_screen(self):
        """/rename ends with Enter, which on Codex's trust screen picks "Yes":
        typed there, it would trust the folder with nobody asked."""
        from unittest import mock
        from xsm import workers
        typed = []
        calls = []
        def screen(pane):
            calls.append(pane)
            return "Do you trust the contents of this directory?" if len(calls) <= 2 else "> ready"
        w = {"name": "cx", "runtime": "codex", "mode": "pane", "pane": "%3", "pid": None,
             "home": self.tmp, "created": 0, "needs_rename": True}
        with mock.patch.object(workers, "_screen", screen), \
                mock.patch.object(workers, "_tmux_type", lambda pane, text: typed.append(text)), \
                mock.patch.object(workers, "_register_named_thread", lambda wk: None), \
                mock.patch.object(workers.time, "sleep", lambda s: None):
            with self.assertRaises(workers.WorkerError):
                workers._wait_for_registration(w, 0.2)
        self.assertEqual(typed, ["/rename cx"], "typed once, after the trust screen was gone")

    def test_two_background_spawns_at_once_both_get_a_window(self):
        """The loser of the race to create xsm-workers opens a window in it."""
        from unittest import mock
        from xsm import workers
        seen = []
        def run(argv, **kw):
            seen.append(argv)
            if argv[1] == "has-session":
                return mock.Mock(returncode=1)
            if argv[1] == "new-session":
                return mock.Mock(returncode=1, stdout="", stderr="duplicate session: xsm-workers")
            return mock.Mock(returncode=0, stdout="%5 4242\n", stderr="")
        w = {"name": "r", "runtime": "claude", "home": self.tmp, "cwd": self.tmp,
             "mode": "background", "model": "m", "approval_timeout": 5}
        os.makedirs(os.path.join(self.tmp, "workers", "r"), exist_ok=True)
        with mock.patch.object(workers.subprocess, "run", run):
            workers._start_in_tmux(w, None)
        self.assertEqual([a[1] for a in seen if a[0] == "tmux"][-2:], ["new-session", "new-window"])
        self.assertEqual(w["pane"], "%5")

    def test_a_background_codex_worker_can_reach_peers_and_its_mcp_tools(self):
        """Its sandbox may open Claude inbox sockets, and xsm's MCP tools need no
        approval — under `-a never` a tool that asks is refused. Pane workers,
        whose person answers in the pane, get neither."""
        import shlex
        from unittest import mock
        from xsm import install, workers
        for mode, expect in (("background", True), ("pane", False)):
            seen = []
            def run(argv, **kw):
                seen.append(argv)
                if argv[1] == "has-session":
                    return mock.Mock(returncode=1)
                return mock.Mock(returncode=0, stdout="%4 4242\n", stderr="")
            w = {"name": "cx-" + mode, "runtime": "codex", "home": self.tmp, "cwd": self.tmp,
                 "mode": mode, "model": "m", "approval_timeout": 5, "created": 0}
            with mock.patch.object(workers.subprocess, "run", run), \
                    mock.patch.object(workers.time, "sleep", lambda s: None):
                workers._start_in_tmux(w, "%1" if mode == "pane" else None)
            launch = next(a for a in seen if a[0] == "tmux" and a[1] in ("split-window", "new-session"))
            command = " ".join(shlex.split(launch[-1]))
            self.assertEqual("sandbox_workspace_write.writable_roots" in command, expect, mode)
            self.assertEqual("mcp_servers.%s.env={XSM_HOME=" % install.MCP_NAME in command, expect,
                             "the xsm MCP server must look in the worker's store, not ~/.xsm")
            self.assertEqual('mcp_servers.%s.default_tools_approval_mode="approve"'
                             % install.MCP_NAME in command, expect, mode)

    def test_a_background_claude_worker_carries_the_xsm_mcp_server_itself(self):
        """Whatever the user's registration: the route to a Codex peer."""
        import json as _json
        from xsm import install, workers
        for mode, expect in (("background", True), ("pane", False)):
            argv = workers._claude_argv({"name": "w", "mode": mode}, "/s.json")
            self.assertEqual("--mcp-config" in argv, expect, mode)
            if expect:
                cfg = _json.loads(argv[argv.index("--mcp-config") + 1])
                self.assertEqual(cfg["mcpServers"][install.MCP_NAME]["args"],
                                 install.mcp_command()[1:])
                from xsm import paths
                self.assertEqual(cfg["mcpServers"][install.MCP_NAME]["env"], {"XSM_HOME": paths.HOME})

    def test_the_default_claude_home_is_never_named_to_claude(self):
        """CLAUDE_CONFIG_DIR=~/.claude sends Claude to ~/.claude/.claude.json,
        which no session reads: folder trust and the MCP registration both
        vanished that way."""
        from unittest import mock
        from xsm import install
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/elsewhere"}):
            self.assertNotIn("CLAUDE_CONFIG_DIR",
                             install._runtime_env(os.path.expanduser("~/.claude"), "claude"))
            self.assertEqual(install._runtime_env("/x/.claude-2", "claude")["CLAUDE_CONFIG_DIR"],
                             "/x/.claude-2")

    def test_trust_keys_are_the_measured_ones(self):
        """Claude's cursor starts on "No, exit": a bare Enter there refuses."""
        from xsm import workers
        self.assertEqual(workers.TRUST_PROMPTS["claude"][1], {"yes": ["Down", "Enter"],
                                                             "no": ["Enter"]})
        self.assertEqual(workers.TRUST_PROMPTS["codex"][1], {"yes": ["Enter"], "no": ["2"]})

    def test_full_access_claude_skips_the_approval_hook(self):
        from xsm import workers
        w = {"name": "c", "mode": "background", "full_access": True, "approval_timeout": 5}
        os.makedirs(os.path.join(self.tmp, "workers", "c"))
        argv = workers._claude_argv(w, workers._claude_worker_settings(w))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        settings = json.load(open(os.path.join(self.tmp, "workers", "c", "settings.json")))
        self.assertNotIn("hooks", settings)


class NoIdleWorkerTest(TempState):
    """A worker blocked on a permission gets someone asked at once, and a
    worker's task tells it not to end on a permission excuse."""

    def test_parent_is_tasked_and_told_the_outcome(self):
        from xsm import registry, workers
        rec = registry.upsert("claude", self.tmp, "s-w", os.getpid(), self.tmp)
        w = {"name": "w1", "runtime": "claude", "mode": "background", "session_id": "s-w",
             "parent_ref": "pppppp", "approval_timeout": 1}
        workers.save(w)
        sent = []
        from xsm import send
        send.send = lambda target, text, **kw: sent.append((target, kw["kind"], text))
        os.environ["XSM_WORKER"] = "w1"
        try:
            workers.permission_request({"tool_name": "Bash", "tool_input": {"command": "rm x"}},
                                       "claude")
        finally:
            del os.environ["XSM_WORKER"]
        self.assertEqual([(t, k) for t, k, _ in sent], [("ref:pppppp", "task"),
                                                         ("ref:pppppp", "note")])
        self.assertIn("xsm_approve", sent[0][2])
        self.assertIn("nobody answered", sent[1][2])

    def test_only_the_parent_can_relay_an_answer(self):
        from xsm import paths, workers
        workers.save({"name": "w1", "parent_ref": "pppppp", "created": 0})
        os.makedirs(paths.path(workers.APPROVALS), exist_ok=True)
        paths.write_json(paths.path(workers.APPROVALS, "r1.json"),
                         {"id": "r1", "worker": "w1", "status": "pending", "summary": "x"})
        with self.assertRaises(workers.WorkerError):
            workers.answer_asked("r1", True, "other1")
        self.assertEqual(workers.answer_asked("r1", True, "pppppp")["status"], "approved")

    def test_mcp_approve_shows_the_request_and_passes_the_answer(self):
        import io
        from xsm import mcp, paths, workers
        me = {"ref": "pppppp", "name": "boss", "alias": "claude-4", "runtime": "claude",
              "cwd": self.tmp}
        workers.save({"name": "w1", "parent_ref": "pppppp", "created": 0})
        os.makedirs(paths.path(workers.APPROVALS), exist_ok=True)
        paths.write_json(paths.path(workers.APPROVALS, "r1.json"),
                         {"id": "r1", "worker": "w1", "status": "pending", "t": 1,
                          "summary": "Bash: npm test"})
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"capabilities": {"elicitation": {}}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "xsm_approve", "arguments": {}}},
                {"jsonrpc": "2.0", "id": "xsm-1",
                 "result": {"action": "accept", "content": {"answer": "allow"}}}]
        out = io.StringIO()
        server = mcp.Server(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        server.session = lambda: me
        server.serve()
        ask = next(json.loads(l) for l in out.getvalue().splitlines() if "elicitation" in l)
        self.assertIn("Bash: npm test", ask["params"]["message"])
        self.assertEqual(paths.read_json(paths.path(workers.APPROVALS, "r1.json"))["status"],
                         "approved")

    def test_a_workers_task_carries_the_no_excuse_rule(self):
        from xsm import envelope
        sender = {"name": "boss", "alias": "claude-4", "ref": "pppppp", "session_id": "s"}
        parsed = envelope.parse(envelope.build("do it", msg_id="m1", sender=sender,
                                               scope="dir:x", kind="task"))
        ctx = envelope.sender_context(parsed, worker=True, cwd="/w/proj")
        self.assertIn("do not end with", ctx)
        self.assertIn("Report only what you actually did", ctx)
        self.assertIn("Your working folder is /w/proj", ctx)
        self.assertNotIn("do not end with", envelope.sender_context(parsed, worker=False))


class Adr0009FixesTest(TempState):
    """The four fixes ADR-0009 required before it closes."""

    def _cli(self, *argv):
        from xsm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
                err.write(str(exc.code))
        return code, out.getvalue() + err.getvalue()

    def test_an_agent_cannot_join_or_speak_for_another_folder(self):
        from xsm import workers
        workers.human_terminal = lambda: False
        code, text = self._cli("join", "demo")
        self.assertEqual(code, 2)
        self.assertIn("xsm_join", text)
        code, text = self._cli("post", "hi", "--dir", self.tmp)
        self.assertIn("only a person", text)

    def test_block_is_open_to_anyone_unblock_only_to_a_person(self):
        from xsm import config, workers
        workers.human_terminal = lambda: False
        self._cli("block", "abcdef")
        self.assertIn("abcdef", config.blocked())
        code, _ = self._cli("unblock", "abcdef")
        self.assertEqual(code, 2)
        self.assertIn("abcdef", config.blocked())
        workers.human_terminal = lambda: True
        self._cli("unblock", "abcdef")
        self.assertNotIn("abcdef", config.blocked())

    def test_send_refuses_a_blocked_target(self):
        from xsm import config, registry, send
        home = os.path.join(self.tmp, "codex")
        os.makedirs(home)
        me = registry.upsert("codex", home, "me", os.getpid(), self.tmp, name="me")
        registry.upsert("codex", home, "you", os.getpid(), self.tmp, name="you")
        target = registry.by_session("codex", "you")
        config.block(target["ref"])
        result = send.send("ref:%s" % target["ref"], "hi", sender=registry.by_session("codex", "me"))
        self.assertEqual(result.status, "refused")
        self.assertIn("blocked", result.reason)

    def test_a_worker_outside_the_callers_scope_needs_a_grant(self):
        from unittest import mock
        from xsm import workers
        workers.human_terminal = lambda: False
        workers._check_installed = lambda home, runtime: None
        workers.check_concurrency = lambda caller: None
        here, there = os.path.join(self.tmp, "here"), os.path.join(self.tmp, "there")
        os.makedirs(here)
        os.makedirs(there)
        caller = {"ref": "pppppp", "cwd": here, "runtime": "claude"}
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "HERDR_", "TMUX"))}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(workers.WorkerError) as cm:
                workers.spawn("claude", cwd=there, caller=caller, background=True)
        self.assertIn("outside-scope", str(cm.exception))
