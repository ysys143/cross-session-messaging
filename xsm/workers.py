"""Workers: sessions xsm starts, hands a task, and stops.

A worker is an ordinary session of either runtime that xsm launched on a
caller's behalf. Once it registers it is addressed like any other session;
what this module adds is the part around it — starting it, answering the
approvals it cannot ask a person for, and stopping it.

Where it runs follows from where the caller is (user decision, 2026-09-21):

- Inside tmux, the worker is the real TUI in a pane split off the caller's own
  pane. A person can watch it and answer its prompts there, and closing the
  pane ends it.
- Anywhere else it runs headless. Claude runs `claude -p` in stream-json mode,
  reading turns from a FIFO it holds open itself, so no process has to stay
  behind to keep it alive (measured: the FIFO opened read-write never reaches
  EOF). Codex has no long-running headless mode, so each turn is a
  `codex exec resume`, run by a short-lived pump that exits when the worker's
  inbox is empty.
- Inside a multi-agent framework (Orca, herdr) xsm starts and stops nothing:
  managing workers belongs to the framework, and xsm only carries the
  cross-session messages the framework does not (user decision, 2026-09-21).

A headless worker has nobody at its screen, so its permission prompts go
through xsm: its PermissionRequest hook records the request, tells the caller,
and waits for a person to answer with `xsm approve`/`xsm deny` in a terminal.
The caller's agent is told, never asked — an agent approving its own worker is
the permission laundering the skill forbids.
"""
from __future__ import annotations

import glob
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

from . import config, envelope, identity, install, paths, registry

WORKERS = "workers"
APPROVALS = "approvals"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
APPROVAL_TIMEOUT = 600          # seconds a headless worker waits for a person
CODEX_MODEL = "gpt-5.6-luna"    # the default for tests; --model overrides

# How each framework marks the terminals it owns. Pane-level variables, not
# app-level ones, so a shell merely started near the app does not count.
FRAMEWORKS = (
    ("orca", ("ORCA_TERMINAL_HANDLE", "ORCA_PANE_KEY")),
    ("herdr", ("HERDR_PANE_ID", "HERDR_ENV")),
)


class WorkerError(Exception):
    pass


# --- where are we ------------------------------------------------------------

def framework_host(env=None) -> str | None:
    env = os.environ if env is None else env
    for name, keys in FRAMEWORKS:
        if any(env.get(k) for k in keys):
            return name
    return None


def refuse_inside_framework() -> None:
    host = framework_host()
    if host:
        raise WorkerError(
            "this terminal belongs to %s, which owns starting and stopping workers here; "
            "use %s for that. xsm still carries messages between sessions." % (host, host))


def tmux_pane(env=None) -> str | None:
    """The tmux pane the caller runs in, if that pane still exists. TMUX leaks
    into processes started from a tmux shell long after the server is gone, so
    the variable alone is not proof."""
    env = os.environ if env is None else env
    pane = env.get("TMUX_PANE")
    if not (env.get("TMUX") and pane and shutil.which("tmux")):
        return None
    try:
        out = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return pane if out.returncode == 0 and out.stdout.strip() == pane else None


# --- records -------------------------------------------------------------------

def _dir(name: str) -> str:
    return paths.path(WORKERS, name)


def _record_path(name: str) -> str:
    return paths.path(WORKERS, name + ".json")


def load(name: str) -> dict | None:
    return paths.read_json(_record_path(name))


def save(record: dict) -> None:
    os.makedirs(paths.path(WORKERS), mode=0o700, exist_ok=True)
    paths.write_json(_record_path(record["name"]), record)


def all_workers() -> list:
    rows = [paths.read_json(p) for p in glob.glob(paths.path(WORKERS, "*.json"))]
    return sorted([r for r in rows if r], key=lambda r: r.get("created", 0))


_INDEX = {"key": None, "rows": []}


def for_session(session_id: str | None) -> dict | None:
    """Called for every Codex record on every lookup, so the worker files are
    read again only when the directory changed (records are written by
    rename, which changes it)."""
    if not session_id:
        return None
    try:
        key = os.stat(paths.path(WORKERS)).st_mtime_ns
    except OSError:
        return None
    if _INDEX["key"] != key:
        _INDEX.update(key=key, rows=all_workers())
    return next((w for w in _INDEX["rows"] if w.get("session_id") == session_id), None)


def is_headless_codex(worker: dict | None) -> bool:
    return bool(worker) and worker.get("runtime") == "codex" and worker.get("mode") == "headless"


def state(worker: dict) -> str:
    if is_headless_codex(worker):
        return "idle" if not _pump_alive(worker) else "working"
    pid = worker.get("pid")
    if not pid or not identity.pid_alive(pid) or identity.lstart(pid) != worker.get("lstart"):
        return "gone"
    return "running"


# --- starting ------------------------------------------------------------------

def _default_home(runtime: str, caller: dict | None) -> str:
    if caller and caller.get("runtime") == runtime and caller.get("home"):
        return caller["home"]
    env = "CLAUDE_CONFIG_DIR" if runtime == "claude" else "CODEX_HOME"
    return os.path.realpath(os.path.expanduser(
        os.environ.get(env) or ("~/.claude" if runtime == "claude" else "~/.codex")))


def _check_installed(home: str, runtime: str) -> None:
    plan = install.plan(home, runtime)
    missing = [a["event"] for a in plan.get("actions", []) if a["action"] == "add"]
    core = {"SessionStart", "UserPromptSubmit"}
    if plan.get("error") or core & set(missing):
        raise WorkerError("xsm is not installed in %s, so a worker there could not be reached; "
                          "run: xsm install --%s-home %s" % (home, runtime, home))


def _hook_command() -> str:
    return install.hook_command("claude", "PermissionRequest")


def _claude_worker_settings(worker: dict) -> str:
    """Settings only this worker loads. `accept` so the caller's messages are
    not held for a person by Claude's own mode check; the PermissionRequest hook
    so a headless worker's prompts reach a person through xsm."""
    settings = {"crossSessionInbound": "accept"}
    if worker["mode"] == "headless":
        settings["hooks"] = {"PermissionRequest": [{"hooks": [{
            "type": "command", "command": _hook_command(),
            "timeout": int(worker["approval_timeout"]) + 30}]}]}
    path = os.path.join(_dir(worker["name"]), "settings.json")
    paths.write_json(path, settings, mode=0o600)
    return path


def _env(worker: dict) -> dict:
    env = dict(os.environ)
    # A worker is not part of the framework or tmux client it was started from.
    for _, keys in FRAMEWORKS:
        for k in keys:
            env.pop(k, None)
    env["XSM_WORKER"] = worker["name"]
    env["CLAUDE_CONFIG_DIR" if worker["runtime"] == "claude" else "CODEX_HOME"] = worker["home"]
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET"):
        env.pop(k, None)                # the caller's identity must not leak in
    return env


def _claude_argv(worker: dict, settings: str) -> list:
    # Reporting back must not wait for a person, in a pane or headless. Only
    # `send`: spawn, stop, install and the rest still ask, so a task cannot use
    # the worker to act on other sessions or on configuration unseen.
    argv = ["claude", "--name", worker["name"], "--settings", settings,
            "--allowedTools", "Bash(%s send:*)" % install.launcher()]
    if worker.get("model"):
        argv += ["--model", worker["model"]]
    if worker.get("effort"):
        argv += ["--effort", worker["effort"]]
    if worker["mode"] == "headless":
        argv += ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                 "--verbose", "--permission-mode", "default"]
    return argv


def _codex_config_args(worker: dict) -> list:
    args = ["-m", worker.get("model") or CODEX_MODEL]
    if worker.get("effort"):
        args += ["-c", 'model_reasoning_effort="%s"' % worker["effort"]]
    return args


def depth_budget(caller: dict | None, requested: int | None = None) -> tuple:
    """(depth of the new worker, the limit that will govern its own spawns).

    A top-level session's limit is `requested`, else XSM_MAX_DEPTH, else
    config `max_depth` (default 1). A worker's limit is the one it was given,
    and a worker can only narrow it for what it starts: the budget lives in the
    worker's record, not in an environment variable the worker could reset."""
    parent = None
    if os.environ.get("XSM_WORKER"):
        parent = load(os.environ["XSM_WORKER"])
    if not parent and caller:
        parent = for_session(caller.get("session_id"))
    if parent:
        inherited = int(parent.get("max_depth", 1))
        if requested is not None and requested > inherited:
            raise WorkerError("worker %s may not raise the depth limit above %d"
                              % (parent["name"], inherited))
        limit, depth = (requested if requested is not None else inherited), \
            int(parent.get("depth", 1)) + 1
        who = "worker %s (depth %d)" % (parent["name"], depth - 1)
    else:
        env_limit = os.environ.get("XSM_MAX_DEPTH")
        limit = requested if requested is not None else \
            int(env_limit) if env_limit and env_limit.isdigit() else \
            int(config.load().get("max_depth", 1))
        depth, who = 1, "this session"
    if depth > limit:
        raise WorkerError("%s may not start a worker: that would be depth %d and the limit is "
                          "%d (max_depth)" % (who, depth, limit))
    return depth, limit


def spawn(runtime: str, *, name: str | None = None, model: str | None = None,
          effort: str | None = None, cwd: str | None = None, home: str | None = None,
          once: bool = False, headless: bool = False, approval_timeout: int = APPROVAL_TIMEOUT,
          wait: float = 90.0, caller: dict | None = None,
          max_depth: int | None = None) -> dict:
    refuse_inside_framework()
    depth, limit = depth_budget(caller, max_depth)
    if runtime not in ("claude", "codex"):
        raise WorkerError("runtime must be claude or codex")
    name = name or "w-%s" % uuid.uuid4().hex[:4]
    if not NAME_RE.match(name):
        raise WorkerError("worker names are letters, digits, '.', '_' and '-' (at most 64)")
    if load(name):
        raise WorkerError("a worker named %s already exists; stop it first or pick another name"
                          % name)
    taken = [r for r in registry.records()
             if r.get("state") == "live" and identity.normalize(r.get("name") or "")
             == identity.normalize(name)]
    if taken:
        raise WorkerError("a live session is already called %s" % name)
    home = os.path.realpath(os.path.expanduser(home)) if home else _default_home(runtime, caller)
    _check_installed(home, runtime)
    cwd = os.path.realpath(os.path.expanduser(cwd or (caller or {}).get("cwd") or os.getcwd()))
    pane = None if headless else tmux_pane()
    worker = {"name": name, "runtime": runtime, "home": home, "model": model, "effort": effort,
              "cwd": cwd, "mode": "pane" if pane else "headless", "once": bool(once),
              "approval_timeout": int(approval_timeout), "created": time.time(),
              "parent_ref": (caller or {}).get("ref"), "session_id": None,
              "depth": depth, "max_depth": limit}
    if runtime == "codex" and not worker["model"]:
        worker["model"] = CODEX_MODEL
    if runtime == "codex":
        worker["env"] = {k: os.environ[k] for k in PUMP_ENV if k in os.environ}
    os.makedirs(_dir(name), mode=0o700, exist_ok=True)
    try:
        if pane:
            _start_in_pane(worker, pane)
        elif runtime == "claude":
            _start_claude_headless(worker)
        else:
            _start_codex_headless(worker, wait)
        save(worker)
        _wait_for_registration(worker, wait)
    except BaseException:
        if worker.get("pane"):
            subprocess.run(["tmux", "kill-pane", "-t", worker["pane"]], capture_output=True,
                           timeout=5)
        if worker.get("pid") and worker.get("lstart"):
            _terminate(worker["pid"], worker["lstart"])
        _cleanup(worker)
        raise
    save(worker)
    return worker


def _start_in_pane(worker: dict, pane: str) -> None:
    env = _env(worker)
    assignments = " ".join("%s=%s" % (k, shlex.quote(env[k]))
                           for k in ("XSM_WORKER", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                                     "XSM_HOME") if k in env)
    unset = " ".join("-u %s" % k for _, keys in FRAMEWORKS for k in keys) + \
        " -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_MESSAGING_SOCKET"
    if worker["runtime"] == "claude":
        argv = _claude_argv(worker, _claude_worker_settings(worker))
    else:
        # The pane is where a person answers, so the worker asks there even if
        # the user's own config runs Codex without approvals.
        argv = ["codex"] + _codex_config_args(worker) + ["-s", "workspace-write",
                                                        "-a", "on-request"]
    # exec all the way down, so the pane's pid is the worker's own pid.
    command = "exec env %s %s %s" % (unset, assignments, " ".join(shlex.quote(a) for a in argv))
    out = subprocess.run(["tmux", "split-window", "-t", pane, "-h", "-d", "-P", "-F",
                          "#{pane_id} #{pane_pid}", "-c", worker["cwd"], command],
                         capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise WorkerError("tmux could not split the pane: %s" % out.stderr.strip())
    pane_id, pid = out.stdout.split()
    worker.update({"pane": pane_id, "pid": int(pid), "lstart": identity.lstart(int(pid))})
    if worker["runtime"] == "codex":
        # A Codex thread exists only once it is named or prompted; naming it
        # lets xsm register it before its first prompt (see adopt_open_codex).
        time.sleep(4)
        _tmux_type(pane_id, "/rename %s" % worker["name"])


def _tmux_type(pane_id: str, text: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", text], timeout=5)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], timeout=5)


def _start_claude_headless(worker: dict) -> None:
    d = _dir(worker["name"])
    fifo = os.path.join(d, "in")
    if not os.path.exists(fifo):
        os.mkfifo(fifo, 0o600)
    # Opened read-write by the worker itself: there is always a writer, so the
    # worker never sees EOF when a sender closes its end.
    fd = os.open(fifo, os.O_RDWR)
    try:
        with open(os.path.join(d, "out.jsonl"), "ab") as out, \
                open(os.path.join(d, "err.log"), "ab") as err:
            proc = subprocess.Popen(_claude_argv(worker, _claude_worker_settings(worker)),
                                    stdin=fd, stdout=out, stderr=err, cwd=worker["cwd"],
                                    env=_env(worker), start_new_session=True)
    finally:
        os.close(fd)
    worker.update({"pid": proc.pid, "lstart": identity.lstart(proc.pid)})


# How to answer each kind of approval request the Codex app-server sends.
# Measured 2026-09-21: `codex exec` never asks (approval_policy is forced to
# "never"), but the app-server — the interface IDE clients use — sends these
# requests to its client and waits, so a headless worker driven through it
# can ask a person.
APPROVAL_ANSWERS = {
    "item/commandExecution/requestApproval": ({"decision": "accept"}, {"decision": "decline"}),
    "item/fileChange/requestApproval": ({"decision": "accept"}, {"decision": "decline"}),
    "execCommandApproval": ({"decision": "approved"}, {"decision": "denied"}),
    "applyPatchApproval": ({"decision": "approved"}, {"decision": "denied"}),
}


def _approval_summary(method: str, params: dict) -> tuple:
    if "commandExecution" in method or method == "execCommandApproval":
        command = params.get("command")
        tool, detail = "shell", " ".join(command) if isinstance(command, list) else command
    elif "fileChange" in method or method == "applyPatchApproval":
        tool = "file change"
        detail = params.get("grantRoot") or ", ".join(sorted((params.get("fileChanges") or {})))
    else:
        tool, detail = "permissions", json.dumps(params.get("permissions"), ensure_ascii=False)
    if params.get("reason"):
        detail = "%s (%s)" % (detail, params["reason"])
    return tool, detail or "?"


class _AppServer:
    """One `codex app-server` process speaking newline-delimited JSON-RPC over
    stdio. It lives for one turn: started by the pump, closed when the turn
    completes, so nothing stays behind between turns."""

    def __init__(self, worker: dict, log):
        self.worker, self.log, self.next_id = worker, log, 0
        d = _dir(worker["name"])
        self.proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=open(os.path.join(d, "err.log"), "ab"),
                                     cwd=worker["cwd"], env=_pump_env(worker), text=True,
                                     bufsize=1)

    def send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        rid = self.next_id
        self.send({"id": rid, "method": method, "params": params})
        while True:
            msg = self.read()
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise WorkerError("codex app-server %s failed: %s" % (method, msg["error"]))
                return msg.get("result") or {}
            self.handle(msg)

    def read(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise WorkerError("codex app-server exited; see %s"
                              % os.path.join(_dir(self.worker["name"]), "err.log"))
        self.log.write(line if line.endswith("\n") else line + "\n")
        self.log.flush()
        return json.loads(line)

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        if not method or "id" not in msg:
            return                                   # a notification; the caller reads those
        if method in APPROVAL_ANSWERS:
            tool, detail = _approval_summary(method, msg.get("params") or {})
            allow, _ = _await_person(self.worker, "codex", tool, detail)
            self.send({"id": msg["id"], "result": APPROVAL_ANSWERS[method][0 if allow else 1]})
        elif method == "item/permissions/requestApproval":
            params = msg.get("params") or {}
            allow, _ = _await_person(self.worker, "codex", *_approval_summary(method, params))
            self.send({"id": msg["id"], "result": {
                "permissions": params.get("permissions") if allow else {}}})
        elif method == "mcpServer/elicitation/request":
            self.send({"id": msg["id"], "result": {"action": "decline"}})
        elif method == "item/tool/requestUserInput":
            self.send({"id": msg["id"], "result": {"answers": {}}})
        else:
            self.send({"id": msg["id"], "error": {"code": -32601,
                                                  "message": "xsm cannot answer %s" % method}})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            self.proc.kill()


def _codex_exec(worker: dict, prompt: str, resume: bool, timeout: float | None = None) -> list:
    """One headless Codex turn through the app-server. Returns the items it
    completed, as {"type": "item.completed", "item": {...}} events.

    Every turn states its sandbox and approval policy: nothing falls back to the
    user's config, which may be full access (measured: `exec resume` has no
    --sandbox option and resumed turns ran danger-full-access)."""
    d = _dir(worker["name"])
    policy = {"approvalPolicy": "on-request", "sandbox": "workspace-write",
              "model": worker.get("model") or CODEX_MODEL, "cwd": worker["cwd"]}
    events, deadline = [], (time.time() + timeout) if timeout else None
    with open(os.path.join(d, "out.jsonl"), "a", encoding="utf-8") as log:
        server = _AppServer(worker, log)
        try:
            server.request("initialize", {"clientInfo": {"name": "xsm", "version": "1"}})
            server.send({"method": "initialized"})
            if resume:
                server.request("thread/resume", dict(policy, threadId=worker["session_id"]))
                thread = worker["session_id"]
            else:
                started = server.request("thread/start", policy)
                thread = (started.get("thread") or {}).get("id")
                events.append({"type": "thread.started", "thread_id": thread})
            turn = {"threadId": thread, "input": [{"type": "text", "text": prompt}],
                    "approvalPolicy": "on-request"}
            if worker.get("effort"):
                turn["effort"] = worker["effort"]
            server.request("turn/start", turn)
            while True:
                if deadline and time.time() > deadline:
                    raise WorkerError("codex did not finish its first turn within %ds; see %s"
                                      % (timeout, os.path.join(d, "err.log")))
                msg = server.read()
                server.handle(msg)
                if msg.get("method") == "item/completed":
                    item = (msg.get("params") or {}).get("item") or {}
                    if item.get("type") == "agentMessage":
                        events.append({"type": "item.completed",
                                       "item": {"type": "agent_message", "text": item.get("text")}})
                if msg.get("method") == "turn/completed":
                    return events
        finally:
            server.close()


def _start_codex_headless(worker: dict, wait: float) -> None:
    events = _codex_exec(worker, (
        "You are an xsm worker session named %s. Tasks will arrive as messages from other "
        "sessions. Reply with exactly: ready" % worker["name"]), resume=False, timeout=wait)
    thread = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
    if not thread:
        raise WorkerError("codex did not start a thread; see %s"
                          % os.path.join(_dir(worker["name"]), "err.log"))
    worker.update({"session_id": thread, "pid": None, "lstart": None})


def _register_named_thread(worker: dict) -> None:
    """A pane worker's thread is the one carrying the name xsm typed into it and
    created after the worker started — exact, where matching a process to the
    newest thread in its folder is a guess."""
    if worker.get("session_id"):
        return
    rows = [t for t in registry._codex_recent_threads(worker["home"], within=3600)
            if t[1] == worker["name"] and t[4] >= worker["created"] - 5]
    if rows:
        thread = max(rows, key=lambda t: t[4])[0]
        worker["session_id"] = thread
        rec = registry.upsert("codex", worker["home"], thread, worker["pid"], worker["cwd"],
                              name=worker["name"])
        rec["adopted"] = True
        paths.write_json(registry._record_path("codex", thread), rec)


def _wait_for_registration(worker: dict, wait: float) -> None:
    deadline = time.time() + wait
    while time.time() < deadline:
        if worker["runtime"] == "codex":
            if worker["mode"] == "pane":
                _register_named_thread(worker)
            rec = next((r for r in registry.records() if r.get("runtime") == "codex" and (
                r.get("session_id") == worker.get("session_id") or
                (worker.get("pid") and r.get("pid") == worker["pid"]))), None)
        else:
            rec = next((r for r in registry.records() if r.get("runtime") == "claude"
                        and r.get("pid") == worker.get("pid") and r.get("state") == "live"
                        and r.get("updated", 0) >= worker["created"] - 1), None)
        if rec:
            worker["session_id"] = rec["session_id"]
            worker["ref"] = rec.get("ref")
            return
        if worker.get("pid") and not identity.pid_alive(worker["pid"]):
            raise WorkerError("the worker exited before registering; see %s"
                              % os.path.join(_dir(worker["name"]), "err.log"))
        time.sleep(0.5)
    raise WorkerError("the worker did not register within %ds (is a trust prompt waiting in "
                      "its pane?)" % wait)


# --- delivering to a headless Codex worker ----------------------------------------

def deliver(worker: dict, content: str) -> None:
    """Hand a message to a headless Codex worker: it becomes the next turn's
    prompt, so the worker's own UserPromptSubmit hook gates it as usual."""
    inbox = os.path.join(_dir(worker["name"]), "inbox")
    os.makedirs(inbox, mode=0o700, exist_ok=True)
    # Names sort in arrival order; milliseconds are too coarse for two sends in a row.
    item = os.path.join(inbox, "%020d-%s.txt" % (time.time_ns(), uuid.uuid4().hex[:6]))
    with open(item + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(item + ".tmp", item)
    ensure_pump(worker)


def _lock_path(worker: dict) -> str:
    return os.path.join(_dir(worker["name"]), "pump.lock")


def _pump_file(worker: dict) -> str:
    return os.path.join(_dir(worker["name"]), "pump.json")


def _try_lock(worker: dict):
    """An exclusive lock held for as long as the returned handle is open. The
    kernel drops it when the pump exits, however it exits."""
    import fcntl
    try:
        os.makedirs(_dir(worker["name"]), mode=0o700, exist_ok=True)
        fh = open(_lock_path(worker), "a")
    except OSError:
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _pump_alive(worker: dict) -> bool:
    fh = _try_lock(worker)
    if fh is None:
        return os.path.exists(_lock_path(worker))
    fh.close()
    return False


def ensure_pump(worker: dict) -> None:
    if _pump_alive(worker):
        return                          # the running pump re-checks the inbox before it lets go
    log = open(os.path.join(_dir(worker["name"]), "err.log"), "ab")
    env = _pump_env(worker)
    env["PYTHONPATH"] = install.REPO
    subprocess.Popen([install.pinned_python(), "-m", "xsm", "pump", worker["name"]],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                     start_new_session=True)
    log.close()


# Whoever sends the message that wakes the pump is some other session; its
# environment (credentials, settings) must not reach the worker's turns.
PUMP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL",
            "TMPDIR", "XSM_HOME", "XSM_PYTHON")


def _pump_env(worker: dict) -> dict:
    env = {k: v for k, v in (worker.get("env") or {}).items()}
    env.update({"XSM_WORKER": worker["name"], "CODEX_HOME": worker["home"]})
    return env


def pump(name: str) -> int:
    """Run queued turns for one headless Codex worker, one at a time, then exit."""
    worker = load(name)
    if not is_headless_codex(worker):
        return 0
    lock = _try_lock(worker)
    if lock is None:
        return 0
    me = os.getpid()
    paths.write_json(_pump_file(worker), {"pid": me, "lstart": identity.lstart(me)})
    inbox = os.path.join(_dir(name), "inbox")
    claimed = os.path.join(_dir(name), "claimed")
    os.makedirs(claimed, mode=0o700, exist_ok=True)
    try:
        while True:
            items = sorted(glob.glob(os.path.join(inbox, "*.txt")))
            if not items:
                lock.close()
                lock = None
                # A message may land after the last look but before the lock
                # goes; its sender saw the lock held and did not start a pump.
                if glob.glob(os.path.join(inbox, "*.txt")):
                    lock = _try_lock(worker)
                    if lock is not None:
                        continue
                return 0
            item = os.path.join(claimed, os.path.basename(items[0]))
            try:
                os.rename(items[0], item)          # claim it; only one pump can
            except OSError:
                continue
            with open(item, encoding="utf-8") as fh:
                content = fh.read()
            try:
                events = _codex_exec(worker, content, resume=True)
            except Exception:
                os.rename(item, items[0])          # put it back rather than lose it
                raise
            os.unlink(item)
            _auto_reply(worker, content, events)
    finally:
        if lock is not None:
            lock.close()
        try:
            os.unlink(_pump_file(worker))
        except OSError:
            pass


def _auto_reply(worker: dict, content: str, events: list) -> None:
    """A headless Codex worker cannot reach xsm from inside its sandbox, so the
    pump sends its final message back for it when the turn answered a task."""
    parsed = envelope.parse(content)
    header = parsed.header
    if header.get("kind") != "task" or not header.get("ref") or not header.get("id"):
        return
    from . import ledger
    if ledger.status(header["id"]).get("status") != "delivered":
        return                          # the worker's gate refused it: nothing was answered
    texts = [e["item"].get("text") for e in events if e.get("type") == "item.completed"
             and (e.get("item") or {}).get("type") == "agent_message" and e["item"].get("text")]
    me = registry.by_session("codex", worker["session_id"])
    if not me:
        return
    from . import send as send_mod
    send_mod.send("ref:%s" % header["ref"], texts[-1] if texts else "(the worker gave no answer)",
                  sender=me, kind="reply", reply_to=header["id"])


# --- approvals --------------------------------------------------------------------

def _approval_path(req_id: str) -> str:
    return paths.path(APPROVALS, req_id + ".json")


def approvals(pending_only: bool = True) -> list:
    rows = [paths.read_json(p) for p in glob.glob(paths.path(APPROVALS, "*.json"))]
    for r in rows:
        if r and r.get("status") == "pending" and not load(r.get("worker") or ""):
            r.update({"status": "denied", "reason": "the worker is gone"})
            paths.write_json(_approval_path(r["id"]), r)
    rows = [r for r in rows if r and (not pending_only or r.get("status") == "pending")]
    return sorted(rows, key=lambda r: r.get("t", 0))


def summarize(tool: str, tool_input) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "url"):
            if tool_input.get(key):
                value = tool_input[key]
                return "%s: %s" % (tool, " ".join(value) if isinstance(value, list) else value)
    return "%s: %s" % (tool, json.dumps(tool_input, ensure_ascii=False)[:200])


def permission_request(data: dict, runtime: str) -> dict | None:
    """The PermissionRequest hook of a headless Claude worker. Silent for
    anything else, so it changes nothing for ordinary sessions."""
    name = os.environ.get("XSM_WORKER")
    worker = load(name) if name else None
    if not worker or worker.get("mode") != "headless":
        return None                     # a pane worker's person answers in the pane
    allow, reason = _await_person(worker, runtime, data.get("tool_name") or "?",
                                  data.get("tool_input"))
    return _decision(allow, reason)


def _await_person(worker: dict, runtime: str, tool: str, tool_input) -> tuple:
    """Record a request, tell the parent, and wait for a person's answer.
    Returns (allowed, reason)."""
    summary = summarize(tool, tool_input) if not isinstance(tool_input, str) \
        else "%s: %s" % (tool, tool_input)
    req = {"id": uuid.uuid4().hex[:8], "worker": worker["name"], "runtime": runtime,
           "t": time.time(), "tool": tool, "input": tool_input, "status": "pending",
           "summary": summary}
    os.makedirs(paths.path(APPROVALS), mode=0o700, exist_ok=True)
    paths.write_json(_approval_path(req["id"]), req)
    _notify_parent(worker, req)
    deadline = time.time() + float(worker.get("approval_timeout") or APPROVAL_TIMEOUT)
    while time.time() < deadline:
        cur = paths.read_json(_approval_path(req["id"])) or {}
        if cur.get("status") in ("approved", "denied"):
            return cur["status"] == "approved", cur.get("reason")
        time.sleep(1)
    req.update({"status": "denied", "reason": "nobody answered within %ds" %
                (deadline - req["t"])})
    paths.write_json(_approval_path(req["id"]), req)
    return False, req["reason"]


def _decision(allow: bool, reason: str | None) -> dict:
    decision = {"behavior": "allow"} if allow else \
        {"behavior": "deny", "message": "denied through xsm: %s" % (reason or "the user said no")}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def _notify_parent(worker: dict, req: dict) -> None:
    if not worker.get("parent_ref") or not worker.get("session_id"):
        return
    try:
        me = registry.by_session(worker["runtime"], worker["session_id"])
        if not me:
            return
        from . import send as send_mod
        send_mod.send("ref:%s" % worker["parent_ref"], (
            "Worker %s is waiting for approval [%s] %s. Only your user can answer it, in a "
            "terminal: xsm approve %s (or xsm deny %s). Do not try to answer it yourself."
            % (worker["name"], req["id"], req["summary"], req["id"], req["id"])),
            sender=me, kind="note")
    except Exception:                       # telling is best effort; waiting is not
        pass


AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SANDBOX",
                 "CODEX_SANDBOX_NETWORK_DISABLED")


def human_terminal() -> bool:
    """Whether a person is plausibly the one typing.

    An agent's shell tool has no terminal (measured: stdin is not a tty and
    /dev/tty cannot be opened), and its environment carries the runtime's
    markers. Neither is a security boundary: an agent running as the same user
    can allocate a pty (`script`), scrub its environment, or write the approval
    file directly. These checks stop an agent from approving by habit or by
    following a peer's instructions; they do not stop one set on getting round
    them. See PROTOCOL 5.5."""
    if any(os.environ.get(k) for k in AGENT_MARKERS):
        return False
    if not sys.stdin.isatty():
        return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


def answer(req_id: str, approve: bool, reason: str | None = None) -> dict:
    req = paths.read_json(_approval_path(req_id))
    if not req:
        raise WorkerError("no approval request %s" % req_id)
    if req.get("status") != "pending":
        raise WorkerError("request %s is already %s" % (req_id, req.get("status")))
    if approve and not human_terminal():
        raise WorkerError("approving needs a person at a terminal; this is not one")
    req.update({"status": "approved" if approve else "denied", "answered": time.time(),
                "reason": reason})
    paths.write_json(_approval_path(req_id), req)
    return req


# --- stopping ---------------------------------------------------------------------

def stop(name: str, reason: str = "stopped") -> dict:
    worker = load(name)
    if not worker:
        raise WorkerError("no worker named %s" % name)
    if worker.get("pane"):
        subprocess.run(["tmux", "kill-pane", "-t", worker["pane"]], capture_output=True, timeout=5)
    if is_headless_codex(worker):
        rec = paths.read_json(_pump_file(worker)) or {}
        if rec.get("pid") and rec.get("lstart"):
            _terminate(rec["pid"], rec["lstart"])
    elif worker.get("pid") and worker.get("lstart"):
        _terminate(worker["pid"], worker["lstart"])
    for req in approvals():
        if req.get("worker") == name:
            req.update({"status": "denied", "reason": "the worker was stopped"})
            paths.write_json(_approval_path(req["id"]), req)
    _cleanup(worker)
    worker["stopped"] = reason
    return worker


def _terminate(pid: int, lstart: str) -> None:
    if not identity.pid_alive(pid) or identity.lstart(pid) != lstart:
        return                              # gone, or the pid now belongs to someone else
    for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pid, sig)             # a headless worker leads its own group
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                return
        deadline = time.time() + wait
        while time.time() < deadline:
            if not identity.pid_alive(pid):
                return
            time.sleep(0.2)


def _cleanup(worker: dict) -> None:
    if worker.get("session_id"):
        pointer = paths.path(paths.SESSIONS, "%s-%s.json" % (worker["runtime"],
                                                              worker["session_id"]))
        try:
            os.unlink(pointer)
        except OSError:
            pass
    shutil.rmtree(_dir(worker["name"]), ignore_errors=True)
    try:
        os.unlink(_record_path(worker["name"]))
    except OSError:
        pass


def on_reply(sender_ref: str | None, reply_to: str | None, receiver: dict | None) -> None:
    """Called by the receiving hook: a `once` worker is stopped when its answer
    to the task it was given reaches the session that started it."""
    if not sender_ref or not receiver:
        return
    for worker in all_workers():
        if worker.get("ref") == sender_ref and worker.get("once") and worker.get("task_id") \
                and worker.get("parent_ref") == receiver.get("ref") \
                and worker.get("task_id") == reply_to:
            # Stopping waits on signals; a hook must not. Hand it to a detached
            # process so the reply's context still reaches the parent in time.
            env = dict(os.environ)
            env["PYTHONPATH"] = install.REPO
            subprocess.Popen([install.pinned_python(), "-m", "xsm", "stop", "--internal",
                              worker["name"]], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                             start_new_session=True)


# --- watching ---------------------------------------------------------------------

def render(event: dict) -> str | None:
    """One line a person can read for one stream event, or None to skip it."""
    t = event.get("type")
    if t == "assistant":                                    # Claude stream-json
        parts = []
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                parts.append("[%s]" % summarize(block.get("name") or "tool", block.get("input")))
        return "\n".join(parts) or None
    if t == "result":
        return "-- turn done --"
    if t == "item.completed":                               # Codex exec --json
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            return item.get("text")
        if item.get("type") == "command_execution":
            return "[shell: %s -> %s]" % (item.get("command"), item.get("exit_code"))
    if t == "turn.completed":
        return "-- turn done --"
    method = event.get("method")                            # Codex app-server
    if method == "item/completed":
        item = (event.get("params") or {}).get("item") or {}
        if item.get("type") == "agentMessage":
            return item.get("text")
        if item.get("type") == "commandExecution":
            return "[shell: %s -> %s]" % (item.get("command"), item.get("exitCode")
                                          if item.get("exitCode") is not None else item.get("status"))
    if method == "turn/completed":
        return "-- turn done --"
    return None


def human_input(worker: dict, text: str) -> None:
    """A line typed in `xsm attach`: the person's own words, so no envelope."""
    if worker["runtime"] == "claude":
        line = json.dumps({"type": "user", "message": {"role": "user", "content": text}})
        fd = os.open(os.path.join(_dir(worker["name"]), "in"), os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
    else:
        deliver(worker, text)


def attach(name: str, out=sys.stdout, inp=sys.stdin, backlog: int = 30) -> int:
    worker = load(name)
    if not worker:
        raise WorkerError("no worker named %s" % name)
    if worker["mode"] == "pane":
        out.write("%s runs in tmux pane %s: tmux select-pane -t %s\n"
                  % (name, worker.get("pane"), worker.get("pane")))
        return 0
    log = os.path.join(_dir(name), "out.jsonl")
    out.write("attached to %s (%s). Type to send it a message; y/n answers a pending "
              "approval; Ctrl-C detaches, the worker keeps running.\n" % (name, worker["runtime"]))
    pos, shown = 0, set()
    lines = open(log, encoding="utf-8").read().splitlines() if os.path.exists(log) else []
    for line in lines[-backlog:]:
        _print_event(line, out)
    pos = os.path.getsize(log) if os.path.exists(log) else 0
    try:
        while load(name):
            if os.path.exists(log) and os.path.getsize(log) > pos:
                with open(log, encoding="utf-8") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                for line in chunk.splitlines():
                    _print_event(line, out)
            pending = [r for r in approvals() if r.get("worker") == name]
            for req in pending:
                if req["id"] not in shown:
                    shown.add(req["id"])
                    out.write("?? approval [%s] %s  (y/n)\n" % (req["id"], req["summary"]))
            out.flush()
            ready, _, _ = select.select([inp], [], [], 0.5)
            if ready:
                text = inp.readline()
                if not text:
                    break
                text = text.strip()
                if pending and text.lower() in ("y", "yes", "n", "no"):
                    answer(pending[0]["id"], text.lower().startswith("y"))
                    out.write("-> %s\n" % ("approved" if text.lower().startswith("y") else "denied"))
                elif text:
                    try:
                        human_input(worker, text)
                    except OSError as exc:
                        out.write("-- %s is not reading (%s) --\n" % (name, exc.strerror))
    except KeyboardInterrupt:
        pass
    out.write("detached from %s\n" % name)
    return 0


def _print_event(line: str, out) -> None:
    try:
        text = render(json.loads(line))
    except ValueError:
        return
    if text:
        out.write(text + "\n")
