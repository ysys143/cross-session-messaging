"""Workers: framework deference, approvals that only a person can grant, and
the headless Codex pump. Live behaviour (spawning real sessions) is covered by
TESTPLAN chapter 10; these tests pin the rules around it."""
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

    def test_stale_tmux_variables_are_not_a_pane(self):
        from xsm import workers
        self.assertIsNone(workers.tmux_pane({"TMUX": "/nonexistent,1,0", "TMUX_PANE": "%999999"}))


class ApprovalTest(TempState):
    def _worker(self, **extra):
        from xsm import workers
        rec = {"name": "w1", "runtime": "claude", "mode": "headless", "approval_timeout": 5,
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


class HeadlessCodexTest(TempState):
    def _worker(self):
        from xsm import registry, workers
        rec = {"name": "cx", "runtime": "codex", "mode": "headless", "session_id": "t-cx",
               "home": self.tmp, "cwd": self.tmp, "created": time.time(), "model": "m"}
        workers.save(rec)
        registry.upsert("codex", self.tmp, "t-cx", os.getpid(), self.tmp)
        return rec

    def test_reachable_between_turns_under_its_worker_name(self):
        from xsm import registry
        self._worker()
        rec = registry.by_session("codex", "t-cx")
        self.assertEqual((rec["name"], rec["state"]), ("cx", "live"))

    def test_pump_runs_each_message_and_answers_tasks(self):
        from xsm import envelope, send, workers
        w = self._worker()
        prompts, replies = [], []
        workers._codex_exec = lambda worker, prompt, resume: prompts.append(prompt) or [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "42"}}]
        send.send = lambda target, body, **kw: replies.append((target, body, kw["kind"],
                                                               kw["reply_to"]))
        sender = {"name": "boss", "alias": "claude-4", "ref": "abcdef", "session_id": "s"}
        task = envelope.build("6*7?", msg_id="m1", sender=sender, scope="dir:x", kind="task")
        note = envelope.build("fyi", msg_id="m2", sender=sender, scope="dir:x", kind="note")
        from xsm import ledger
        blocked = envelope.build("rm -rf", msg_id="m3", sender=sender, scope="dir:x", kind="task")
        ledger.receipt("m1", "delivered", {"name": "cx"})
        ledger.receipt("m3", "held", {"name": "cx"}, "out of scope")
        workers.ensure_pump = lambda worker: None
        for item in (task, note, blocked):
            workers.deliver(w, item)
        workers.pump("cx")
        self.assertEqual(prompts, [task, note, blocked], "one turn per message, in order")
        self.assertEqual(replies, [("ref:abcdef", "42", "reply", "m1")],
                         "only a delivered task gets an automatic answer")
        self.assertEqual(os.listdir(os.path.join(self.tmp, "workers", "cx", "claimed")), [])

    def test_a_failed_turn_puts_the_message_back(self):
        from xsm import workers
        w = self._worker()
        def boom(worker, prompt, resume):
            raise RuntimeError("codex failed")
        workers._codex_exec = boom
        workers.ensure_pump = lambda worker: None
        workers.deliver(w, "hello")
        with self.assertRaises(RuntimeError):
            workers.pump("cx")
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "workers", "cx", "inbox"))), 1)

    def test_a_second_pump_does_not_run_while_one_holds_the_lock(self):
        from xsm import workers
        w = self._worker()
        held = workers._try_lock(w)
        try:
            self.assertTrue(workers._pump_alive(w))
            ran = []
            workers._codex_exec = lambda *a: ran.append(1) or []
            workers.ensure_pump = lambda worker: None
            workers.deliver(w, "x")
            self.assertEqual(workers.pump("cx"), 0)
            self.assertEqual(ran, [])
        finally:
            held.close()
        self.assertFalse(workers._pump_alive(w))

    def test_once_worker_stops_when_its_answer_arrives(self):
        from xsm import workers
        workers.save({"name": "o1", "runtime": "claude", "mode": "headless", "ref": "wwwwww",
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
        workers.save({"name": "o2", "runtime": "claude", "mode": "headless", "ref": "wwwwww",
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


class CodexAppServerTest(TempState):
    """Headless Codex turns go through the app-server: each states its sandbox
    and approval policy, and its approval requests wait for a person."""

    def _setup(self):
        from xsm import workers
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "codex")
        with open(fake, "w") as fh:
            fh.write(FAKE_APP_SERVER.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        os.chmod(fake, 0o755)
        w = {"name": "cx", "runtime": "codex", "mode": "headless", "session_id": "t1",
             "home": self.tmp, "cwd": self.tmp, "model": "m", "approval_timeout": 10,
             "env": {"PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
                     "FAKE_LOG": os.path.join(self.tmp, "fake.log")}}
        workers.save(w)
        os.makedirs(os.path.join(self.tmp, "workers", "cx"), exist_ok=True)
        workers.PUMP_ENV = workers.PUMP_ENV + ("FAKE_LOG",)
        return workers, w

    def _answer(self, workers, approve):
        def run():
            for _ in range(100):
                rows = workers.approvals()
                if rows:
                    workers.human_terminal = lambda: True
                    workers.answer(rows[0]["id"], approve)
                    return
                time.sleep(0.1)
        threading.Thread(target=run).start()

    def test_every_turn_states_its_policy_and_asks_a_person(self):
        workers, w = self._setup()
        self._answer(workers, False)
        events = workers._codex_exec(w, "hi", resume=True)
        self.assertEqual([e["item"]["text"] for e in events], ["declined"])
        sent = [json.loads(l) for l in open(os.path.join(self.tmp, "fake.log"))]
        resume = next(m for m in sent if m.get("method") == "thread/resume")["params"]
        self.assertEqual((resume["approvalPolicy"], resume["sandbox"]),
                         ("on-request", "workspace-write"))
        answer = next(m for m in sent if m.get("id") == "srv-1")
        self.assertEqual(answer["result"], {"decision": "decline"})

    def test_an_approved_request_is_accepted(self):
        workers, w = self._setup()
        self._answer(workers, True)
        events = workers._codex_exec(w, "hi", resume=False)
        self.assertEqual(events[0], {"type": "thread.started", "thread_id": "t-new"})
        self.assertEqual(events[-1]["item"]["text"], "done")


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
