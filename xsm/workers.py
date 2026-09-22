"""Workers: sessions xsm starts, hands a task, and stops.

A worker is an ordinary session of either runtime that xsm launched on a
caller's behalf. Once it registers it is addressed like any other session;
what this module adds is the part around it — starting it, answering the
approvals it cannot ask a person for, and stopping it.

Where it runs follows from where the caller is (user decision, 2026-09-21):

- Inside tmux, the worker is the real TUI in a pane split off the caller's own
  pane. A person can watch it and answer its prompts there, and closing the
  pane ends it.
- Anywhere else, or with --background, it runs in the background: the same
  real TUI, in a window of a detached tmux session named xsm-workers. Nobody is
  at its screen, but it is a live session all the same — it takes messages the
  way any session does.
- Inside a multi-agent framework (Orca, herdr) xsm starts and stops nothing:
  managing workers belongs to the framework, and xsm only carries the
  cross-session messages the framework does not (user decision, 2026-09-21).

A worker is never `claude -p` or `codex exec`. Those run one prompt and are
gone: there is no session to send a message to, so there is nothing for xsm to
connect (user decision, 2026-09-22; an earlier version ran such "headless"
workers, and a headless Claude worker could not even be sent a message).

Nobody is at a background worker's screen, so what it would ask there comes to
a person through xsm instead of the person going to the screen: a folder-trust
screen before it starts, and a background Claude worker's permission prompts.
Its PermissionRequest hook records the request, tells the caller,
and waits for a person to answer with `xsm approve`/`xsm deny` in a terminal. its PermissionRequest hook records the request, tells the caller,
and waits for a person to answer with `xsm approve`/`xsm deny` in a terminal.
The caller's agent is told, never asked — an agent approving its own worker is
the permission laundering the skill forbids.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import nullcontext

from . import config, identity, install, paths, registry

WORKERS = "workers"
APPROVALS = "approvals"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
APPROVAL_TIMEOUT = 600          # seconds a background worker waits for a person
BACKGROUND_SESSION = "xsm-workers"
CLAUDE_SOCKETS = "/tmp/cc-socks"    # where every Claude session opens its inbox socket

# A runtime's "do you trust this folder" screen, and the keys that answer it
# (measured 2026-09-22 in tmux: Claude's cursor starts on "No, exit", Codex's
# on "Yes, continue", so the same Enter means opposite things).
TRUST_PROMPTS = {
    "claude": ("Yes, I trust this folder", {"yes": ["Down", "Enter"], "no": ["Enter"]}),
    "codex": ("Do you trust the contents of this directory", {"yes": ["Enter"], "no": ["2"]}),
}
CODEX_MODEL = "gpt-5.6-luna"    # the default for tests; --model overrides

# How each framework marks the terminals it owns. Pane-level variables, not
# app-level ones, so a shell merely started near the app does not count.
FRAMEWORKS = (
    ("orca", ("ORCA_TERMINAL_HANDLE", "ORCA_PANE_KEY")),
    ("herdr", ("HERDR_PANE_ID", "HERDR_ENV")),
)


# --- what a background worker may do without asking --------------------------
#
# One declaration, because the rule was widened three times in one day, each
# time after a worker sat ten minutes on a question nobody was there to answer
# (S10, 2026-09-22/23): shell commands, then reads outside the folder, then
# xsm_inbox. Both runtimes are configured from this; `xsm workers --policy`
# prints it, and PROTOCOL 5.5 explains each line.
WORKER_POLICY = {
    "shell": "every shell command, because every one runs inside the OS sandbox",
    "read": "read anything, anywhere (Read, Glob, Grep; Codex workspace-write reads too)",
    "write": "write inside the working folder only, plus the xsm store",
    "reach": "message peers: the xsm MCP tools, which run outside the sandbox",
    "ask": "anything else goes to a person: Claude through the PermissionRequest hook, "
           "Codex by refusing (it has no such hook)",
}
CLAUDE_WORKER_TOOLS = ("Bash", "Monitor", "Read", "Glob", "Grep")
CLAUDE_WORKER_MCP = ("xsm_send", "xsm_post", "xsm_channel", "xsm_inbox")


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
    ignored = config.ignored_frameworks()
    if host and not (host in ignored or "all" in ignored):
        raise WorkerError(
            "this terminal belongs to %s, which owns starting and stopping workers here; "
            "use %s for that. xsm still carries messages between sessions. (A person can "
            "let xsm start workers here anyway: `xsm frameworks ignore %s` in a terminal.)"
            % (host, host, host))


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


def state(worker: dict) -> str:
    pid = worker.get("pid")
    if not pid or not identity.pid_alive(pid) or identity.lstart(pid) != worker.get("lstart"):
        return "gone"
    return "running"


# --- explicit permission for dangerous workers ---------------------------------------
#
# --full-access (no sandbox, no approvals) and --trust-hooks (run hooks without
# Codex's trust review) take away what protects the user, so an agent may use
# them only with the user's explicit permission, asked for through the xsm MCP
# tool `xsm_grant` (elicitation: the answer comes from the person, not the
# model). A grant is for one spawn: bound to the asking session, runtime,
# folder and options, and good for GRANT_TTL seconds. A person typing `spawn`
# at a terminal needs no grant: that is the permission (user decision,
# 2026-09-22).

GRANTS = "grants"
GRANT_TTL = 600
DANGEROUS = ("full_access", "trust_hooks", "outside_scope", "remote")


def create_grant(asked_by: str, runtime: str, cwd: str, options: list, answer: str) -> dict:
    grant = {"id": uuid.uuid4().hex[:8], "asked_by": asked_by, "runtime": runtime,
             "cwd": os.path.realpath(cwd), "options": sorted(options), "t": time.time(),
             "expires": time.time() + GRANT_TTL, "answer": answer}
    os.makedirs(paths.path(GRANTS), mode=0o700, exist_ok=True)
    paths.write_json(paths.path(GRANTS, grant["id"] + ".json"), grant)
    return grant


def use_grant(grant_id: str | None, caller: dict | None, runtime: str, cwd: str,
              options: list) -> dict:
    """Consume a grant that covers exactly this spawn, or refuse."""
    if not grant_id:
        raise WorkerError("%s needs your user's explicit permission: ask with the xsm_grant MCP "
                          "tool, then pass --grant <id>. If that call is blocked, ask your user "
                          "whether to request it, and call it again if they agree" % " and ".join(
                              "--" + o.replace("_", "-") for o in options))
    p = paths.path(GRANTS, grant_id + ".json")
    claimed = p + ".used"
    try:
        os.rename(p, claimed)                 # one use: the rename is the claim
    except OSError:
        raise WorkerError("no unused grant %s" % grant_id)
    grant = paths.read_json(claimed) or {}
    problems = []
    if time.time() > grant.get("expires", 0):
        problems.append("it expired")
    if grant.get("asked_by") != (caller or {}).get("ref"):
        problems.append("another session asked for it")
    if grant.get("runtime") != runtime:
        problems.append("it is for %s" % grant.get("runtime"))
    if grant.get("cwd") != os.path.realpath(cwd):
        problems.append("it is for %s" % grant.get("cwd"))
    if not set(options) <= set(grant.get("options") or []):
        problems.append("it does not cover %s" % ", ".join(sorted(set(options) -
                                                                  set(grant.get("options") or []))))
    if problems:
        raise WorkerError("grant %s does not cover this spawn: %s (it is used up now; ask again)"
                          % (grant_id, "; ".join(problems)))
    return grant


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


def _working_codex() -> str:
    """The first codex on this machine that answers `--version`."""
    from . import adapters
    candidates = adapters.codex_bins()
    for path in candidates:
        try:
            ran = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if ran.returncode == 0:
            return path
    if candidates:
        return candidates[0]            # let it fail in the open, with its own words
    raise WorkerError("codex is not installed on this machine")


def _hook_command() -> str:
    return install.hook_command("claude", "PermissionRequest")


def _claude_worker_settings(worker: dict) -> str:
    """Settings only this worker loads. `accept` so the caller's messages are
    not held for a person by Claude's own mode check; the PermissionRequest hook
    so a background worker's prompts reach a person through xsm."""
    settings: dict = {"crossSessionInbound": "accept"}
    if worker["mode"] == "background" and not worker.get("full_access"):
        # The rule a background Codex worker runs under, for Claude: shell
        # commands in the OS sandbox (no writes outside the folder) run without
        # asking, and so do edits inside the folder (--permission-mode
        # acceptEdits). Anything past that still comes to a person through the
        # hook below (user decision, 2026-09-22; measured before it: every
        # `./timeleft` waited for an answer nobody was there to give). Every
        # shell command is allowed because every one runs sandboxed — Claude
        # asks even inside the sandbox about a command with `$var` in it — and
        # xsm too stays inside, with its store the one writable place outside
        # the folder, so a worker cannot run `xsm install` against the user's
        # settings.
        # Sending is the one thing the sandbox must let through: a Claude
        # peer's inbox socket, and a Codex peer's queue database — only that
        # file, not the Codex home, whose config the sandbox keeps shut.
        # Measured without these: `xsm send` from the worker failed with
        # "a sandboxed session cannot open the inbox socket".
        # `codex queue` opens the home's state database as well as the queue
        # (measured: "failed to open state DB ... state_5.sqlite (code: 8)"
        # from inside the sandbox), so every sqlite file there is writable;
        # config.toml and hooks.json stay shut.
        codex_homes = [h["path"] for h in config.homes() if h.get("runtime") == "codex"]
        queues = [p for h in codex_homes for p in glob.glob(os.path.join(h, "*.sqlite*"))]
        # `codex queue` also starts an embedded app server, which needs the
        # home's ipc and daemon folders and a local socket to bind (measured:
        # "failed to start embedded app server: Operation not permitted").
        ipc = [os.path.join(h, d) for h in codex_homes for d in ("ipc", "app-server-daemon")]
        settings["sandbox"] = {"enabled": True, "autoAllowBashIfSandboxed": True,
                               "allowUnsandboxedCommands": False,
                               "filesystem": {"allowWrite": [paths.HOME] + queues + ipc},
                               "network": {"allowUnixSockets": [CLAUDE_SOCKETS,
                                                                os.path.realpath(CLAUDE_SOCKETS)]
                                           + ipc, "allowLocalBinding": True}}
        # Monitor runs shell commands too, under the same sandbox; without it a
        # worker that watched a file with Monitor sat on an approval for ten
        # minutes (S10 collab pilot). The xsm MCP tools run outside the sandbox
        # and are the route to a Codex peer: `codex queue` from the sandboxed
        # shell cannot start its embedded app server (measured), and the MCP
        # server can. Same as the Codex worker's route to a Claude peer.
        # Reading is allowed anywhere, as Codex's workspace-write sandbox
        # allows it: reads outside the folder asked for a person, and a worker
        # checking INTENT.md for its review sat on that for the rest of the run
        # (S10 collab run 4). Writing stays inside the folder.
        # Its shells cannot run `codex queue`; `xsm send` reads this and says
        # so at once instead of failing after trying (send.sandboxed).
        settings["env"] = {"XSM_SANDBOXED": "1"}
        settings["permissions"] = {"allow": list(CLAUDE_WORKER_TOOLS) + [
            "mcp__%s__%s" % (install.MCP_NAME, tool) for tool in CLAUDE_WORKER_MCP]}
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
    if worker["runtime"] == "claude" and os.path.realpath(worker["home"]) == \
            os.path.realpath(os.path.expanduser("~/.claude")):
        # The default home is not named. Claude Code reads folder trust from
        # ~/.claude.json only when CLAUDE_CONFIG_DIR is unset; naming ~/.claude
        # sends it to ~/.claude/.claude.json instead, where no folder is trusted,
        # and every worker stopped at a trust prompt (measured 2026-09-22).
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR" if worker["runtime"] == "claude" else "CODEX_HOME"] = worker["home"]
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET"):
        env.pop(k, None)                # the caller's identity must not leak in
    return env


def _claude_argv(worker: dict, settings: str) -> list:
    # Reporting back must not wait for a person, in a pane or in the background. Only
    # `send`: spawn, stop, install and the rest still ask, so a task cannot use
    # the worker to act on other sessions or on configuration unseen.
    argv = ["claude", "--name", worker["name"], "--settings", settings,
            "--allowedTools", "Bash(%s send:*)" % install.launcher()]
    if worker["mode"] == "background":
        # The xsm MCP server, whatever the user's own registration: a
        # background worker's route to a Codex peer is this server, which runs
        # outside the shell sandbox. (Registration was also found landing in
        # ~/.claude/.claude.json, which no session reads — see install.)
        argv += ["--mcp-config", json.dumps({"mcpServers": {install.MCP_NAME: {
            "command": install.mcp_command()[0], "args": install.mcp_command()[1:],
            "env": {"XSM_HOME": paths.HOME}}}})]
    if worker.get("model"):
        argv += ["--model", worker["model"]]
    if worker.get("effort"):
        argv += ["--effort", worker["effort"]]
    # Never the user's own mode: a pane worker asks its person in the pane
    # (default); a background one works unasked inside its folder and sandbox
    # and asks through xsm past that (acceptEdits + the sandbox settings).
    argv += ["--permission-mode", "bypassPermissions" if worker.get("full_access") else
             "acceptEdits" if worker["mode"] == "background" else "default"]
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


def _limit(key: str, env: str, default: int) -> int:
    value = os.environ.get(env)
    if value and value.isdigit():
        return int(value)
    try:
        return int(config.load().get(key, default))
    except (TypeError, ValueError):
        return default


def check_concurrency(caller: dict | None) -> None:
    """At most max_workers (config, XSM_MAX_WORKERS; default 4) running workers
    per starting session. Orphans are reaped first so they do not count."""
    reap()
    parent = (caller or {}).get("ref")
    running = [w for w in all_workers() if w.get("parent_ref") == parent and state(w) != "gone"]
    limit = _limit("max_workers", "XSM_MAX_WORKERS", 4)
    if len(running) >= limit:
        raise WorkerError("%s already has %d running worker(s) and the limit is %d (max_workers): "
                          "%s. Stop one first: xsm stop <name>" % (
                              "this session" if parent else "this terminal", len(running), limit,
                              ", ".join(w["name"] for w in running)))


def spawn(runtime: str, *, name: str | None = None, model: str | None = None,
          effort: str | None = None, cwd: str | None = None, home: str | None = None,
          once: bool = False, background: bool = False, approval_timeout: int = APPROVAL_TIMEOUT,
          wait: float = 90.0, caller: dict | None = None,
          max_depth: int | None = None, full_access: bool = False, trust_hooks: bool = False,
          grant: str | None = None, task: dict | None = None) -> dict:
    """Start a worker. If it stops at a folder-trust screen in the background,
    return at once with worker["waiting"] set to an approval request: the
    caller is an agent blocked in this call, and it has to be free to ask its
    user. A detached `xsm worker-finish` answers the screen once a person has,
    finishes registration and hands over `task` ({"id", "text"})."""
    refuse_inside_framework()
    depth, limit = depth_budget(caller, max_depth)
    check_concurrency(caller)
    dangerous = [o for o, on in (("full_access", full_access), ("trust_hooks", trust_hooks)) if on]
    if trust_hooks and runtime != "codex":
        raise WorkerError("--trust-hooks is a Codex option; Claude has no hook trust to bypass")
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
    pane = None if background else tmux_pane()
    if runtime == "codex" and not trust_hooks:
        trust = install.codex_trust(home)
        if not trust or not all(trust.get(k) for k in ("SessionStart", "UserPromptSubmit")):
            raise WorkerError("the xsm hooks are not trusted in %s, so the worker could not "
                              "register; start codex there once and trust them" % home)
    if caller and not config.scope_for(caller, {"cwd": cwd})[0]:
        # A worker in a folder outside the caller's scope joins that folder's
        # project and can talk to every session there, who never agreed.
        dangerous.append("outside_scope")
    granted = None
    if dangerous and not human_terminal():
        granted = use_grant(grant, caller, runtime, cwd, dangerous)
    if not shutil.which("tmux"):
        raise WorkerError("a worker runs as a real session in tmux, and tmux is not installed")
    worker = {"name": name, "runtime": runtime, "home": home, "model": model, "effort": effort,
              "cwd": cwd, "mode": "pane" if pane else "background", "once": bool(once),
              "approval_timeout": int(approval_timeout), "created": time.time(),
              "parent_ref": (caller or {}).get("ref"), "session_id": None,
              "depth": depth, "max_depth": limit, "full_access": full_access,
              "trust_hooks": trust_hooks, "grant": (granted or {}).get("id"),
              "pending_task": task}
    if runtime == "codex" and not worker["model"]:
        worker["model"] = CODEX_MODEL
    os.makedirs(_dir(name), mode=0o700, exist_ok=True)
    try:
        from . import telemetry
    except ImportError:
        telemetry = None
    # Only the part that takes time: the checks above either pass at once or
    # raise, and timing them would say nothing about how long a worker takes
    # to come up.
    span_cm = telemetry.span("xsm.worker.spawn",
                             {"xsm.worker.runtime": runtime, "xsm.worker.name": name,
                              "xsm.worker.mode": worker["mode"], "xsm.worker.depth": depth}) \
        if telemetry else nullcontext()
    try:
        with span_cm:
            _start_in_tmux(worker, pane)
            save(worker)
            try:
                _wait_for_registration(worker, wait)
            except _WaitingForTrust:
                _ask_trust(worker)
                save(worker)
                _finish_detached(worker)
                return worker
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


def _start_in_tmux(worker: dict, pane: str | None) -> None:
    """Start the worker's TUI: in a pane split off the caller's, or, with no
    pane to split, in a new window of the detached xsm-workers session."""
    env = _env(worker)
    assignments = " ".join("%s=%s" % (k, shlex.quote(env[k]))
                           for k in ("XSM_WORKER", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                                     "XSM_HOME") if k in env)
    unset = " ".join("-u %s" % k for _, keys in FRAMEWORKS for k in keys) + \
        " -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_MESSAGING_SOCKET"
    if worker["runtime"] == "claude":
        argv = _claude_argv(worker, _claude_worker_settings(worker))
    else:
        # A pane is where a person answers, so a pane worker asks there even if
        # the user's own config runs Codex without approvals. A background one
        # has nobody to ask and Codex has no hook to relay the question, so it
        # does not ask: it stays in its workspace-write sandbox instead.
        asks = ["-a", "on-request"] if worker["mode"] == "pane" else ["-a", "never"]
        # A background Codex worker reports through xsm like any session. Its
        # sandboxed shell cannot open a Claude peer's inbox socket (seatbelt
        # counts that as a filesystem violation, and network.allow_unix_sockets
        # did not change it — measured 2026-09-22), so the route is the xsm MCP
        # server, which runs outside the sandbox: its tools need no approval,
        # since under `-a never` a tool that asks is refused outright. The
        # shell still needs XSM_HOME writable for the ledger it keeps.
        reach = [] if worker.get("full_access") or worker["mode"] == "pane" else [
            "-c", "sandbox_workspace_write.writable_roots=[%s]" % json.dumps(paths.HOME),
            "-c", 'mcp_servers.%s.default_tools_approval_mode="approve"' % install.MCP_NAME,
            # Codex starts MCP servers with an environment of its own, not the
            # worker's: without this the xsm server looked in ~/.xsm and told
            # the worker "this session is not registered" (measured, S10 run 2
            # under a run-local XSM_HOME).
            "-c", 'mcp_servers.%s.env={XSM_HOME=%s}' % (install.MCP_NAME, json.dumps(paths.HOME))]
        # The codex that runs, not whichever is first on PATH: a broken npm
        # install shadowed the working one (2026-09-23), and a worker started
        # from it would die in its pane with nobody watching.
        argv = [_working_codex()] + _codex_config_args(worker) + (
            ["--dangerously-bypass-approvals-and-sandbox"] if worker.get("full_access")
            else ["-s", "workspace-write"] + asks + reach) + (
            ["--dangerously-bypass-hook-trust"] if worker.get("trust_hooks") else [])
    # exec all the way down, so the pane's pid is the worker's own pid.
    command = "exec env %s %s %s" % (unset, assignments, " ".join(shlex.quote(a) for a in argv))
    fmt = ["-P", "-F", "#{pane_id} #{pane_pid}", "-c", worker["cwd"]]
    if pane:
        tmux = ["tmux", "split-window", "-t", pane, "-h", "-d"] + fmt + [command]
    elif subprocess.run(["tmux", "has-session", "-t", BACKGROUND_SESSION],
                        capture_output=True, timeout=5).returncode == 0:
        tmux = ["tmux", "new-window", "-d", "-t", BACKGROUND_SESSION, "-n", worker["name"]] + \
            fmt + [command]
    else:
        tmux = ["tmux", "new-session", "-d", "-s", BACKGROUND_SESSION, "-n", worker["name"],
                "-x", "200", "-y", "50"] + fmt + [command]
    out = subprocess.run(tmux, capture_output=True, text=True, timeout=10)
    if out.returncode != 0 and "duplicate session" in out.stderr:
        # Another spawn created the session between our check and ours
        # (measured: three background spawns at once, two lost this race).
        tmux = ["tmux", "new-window", "-d", "-t", BACKGROUND_SESSION, "-n", worker["name"]] + \
            fmt + [command]
        out = subprocess.run(tmux, capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise WorkerError("tmux could not open the worker's %s: %s" % (
            "pane" if pane else "window", out.stderr.strip()))
    pane_id, pid = out.stdout.split()
    worker.update({"pane": pane_id, "pid": int(pid), "lstart": identity.lstart(int(pid))})
    if worker["runtime"] == "codex":
        # A Codex thread exists only once it is named or prompted; naming it
        # lets xsm register it before its first prompt (see adopt_open_codex).
        # Typed later, from the registration wait: typed now it could land on
        # a folder-trust screen, where its Enter picks the default, "Yes".
        worker["needs_rename"] = True


def _tmux_type(pane_id: str, text: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", text], timeout=5)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], timeout=5)


def _register_named_thread(worker: dict) -> None:
    """A Codex worker's thread is the one carrying the name xsm typed into it and
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


class _WaitingForTrust(Exception):
    pass


def _screen(pane: str) -> str:
    try:
        return subprocess.run(["tmux", "capture-pane", "-p", "-t", pane], capture_output=True,
                              text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _at_trust_prompt(worker: dict) -> bool:
    marker = TRUST_PROMPTS.get(worker["runtime"], ("",))[0]
    return bool(marker and worker.get("pane") and marker in _screen(worker["pane"]))


def _press(worker: dict, answer: str) -> None:
    for key in TRUST_PROMPTS[worker["runtime"]][1][answer]:
        subprocess.run(["tmux", "send-keys", "-t", worker["pane"], key], timeout=5)
        time.sleep(0.5)


def _ask_trust(worker: dict) -> dict:
    """Turn the trust screen into an approval request: the same record, the
    same statusline count and the same xsm_approve form as a permission."""
    req = {"id": uuid.uuid4().hex[:8], "worker": worker["name"], "runtime": worker["runtime"],
           "t": time.time(), "tool": "folder-trust", "input": worker["cwd"], "status": "pending",
           "summary": "trust the folder %s so the %s worker can start" % (worker["cwd"],
                                                                        worker["runtime"])}
    os.makedirs(paths.path(APPROVALS), mode=0o700, exist_ok=True)
    paths.write_json(_approval_path(req["id"]), req)
    worker["waiting"] = req["id"]
    return req


def _finish_detached(worker: dict) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = install.REPO
    subprocess.Popen([install.pinned_python(), "-m", "xsm", "worker-finish", worker["name"]],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=env, start_new_session=True)


def finish(name: str) -> int:
    """Wait for a person's answer to a trust screen, answer the screen, and
    finish what spawn could not: registration, then the pending task. Runs
    detached; says what happened through the approval record, the statusline,
    and — once the worker can send — a note to the parent."""
    while True:
        worker = load(name)
        if not worker or not worker.get("waiting"):
            return 0
        req_id = worker["waiting"]
        deadline = time.time() + float(worker.get("approval_timeout") or APPROVAL_TIMEOUT)
        status = "pending"
        while time.time() < deadline and load(name):
            status = (paths.read_json(_approval_path(req_id)) or {}).get("status") or "denied"
            if status != "pending":
                break
            time.sleep(1)
        if not load(name):
            return 0
        if status != "approved":
            if status == "pending":
                req = paths.read_json(_approval_path(req_id)) or {}
                req.update({"status": "denied", "reason": "nobody answered within %ds"
                            % (deadline - req.get("t", deadline))})
                paths.write_json(_approval_path(req_id), req)
            _press(worker, "no")
            stop(name, reason="folder trust was not given")
            return 0
        _press(worker, "yes")
        worker.pop("waiting", None)
        save(worker)
        try:
            _wait_for_registration(worker, 90)
        except _WaitingForTrust:
            _ask_trust(worker)             # another trust screen: ask again
            save(worker)
            continue
        except WorkerError:
            stop(name, reason="it did not register after folder trust")
            return 0
        save(worker)
        break
    task = worker.pop("pending_task", None)
    if task:
        parent = next((r for r in registry.records() if r.get("ref") == worker.get("parent_ref")),
                      None)
        if parent:
            from . import send as send_mod
            worker["task_id"] = task["id"]
            save(worker)
            send_mod.send("ref:%s" % worker["ref"], task["text"], sender=parent, kind="task",
                          msg_id=task["id"])
    else:
        save(worker)
    return 0


def _wait_for_registration(worker: dict, wait: float) -> None:
    deadline = time.time() + wait
    while time.time() < deadline:
        at_trust = _at_trust_prompt(worker)
        if at_trust and worker.get("mode") == "background":
            raise _WaitingForTrust()
        if worker.get("needs_rename") and not at_trust and \
                time.time() - worker.get("created", 0) >= 4:
            _tmux_type(worker["pane"], "/rename %s" % worker["name"])
            worker.pop("needs_rename")
        if worker["runtime"] == "codex":
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
            raise WorkerError("the worker exited before registering")
        time.sleep(0.5)
    raise WorkerError("the worker did not register within %ds%s" % (
        wait, " (is a trust prompt waiting in its pane?)" if worker["mode"] == "pane" else ""))


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
    """The PermissionRequest hook of a background Claude worker. Silent for
    anything else, so it changes nothing for ordinary sessions."""
    name = os.environ.get("XSM_WORKER")
    worker = load(name) if name else None
    if not worker or worker.get("mode") != "background":
        return None                     # a pane worker's person answers in the pane
    allow, reason = _await_person(worker, runtime, data.get("tool_name") or "?",
                                  data.get("tool_input"))
    return _decision(allow, reason)


def _await_person(worker: dict, runtime: str, tool: str, tool_input) -> tuple:
    """Record a request, tell the parent, and wait for a person's answer.
    Returns (allowed, reason)."""
    try:
        from . import telemetry
    except ImportError:
        telemetry = None
    span_cm = telemetry.span("xsm.worker.approval_wait",
                             {"xsm.worker.name": worker["name"], "xsm.tool": tool}) \
        if telemetry else nullcontext()
    start = time.time()
    allowed, reason = False, None
    try:
        with span_cm as span:
            allowed, reason = _await_person_inner(worker, runtime, tool, tool_input)
            if span is not None:
                span.set_attribute("xsm.approval.allowed", allowed)
            return allowed, reason
    finally:
        if telemetry:
            # How long a background worker sat waiting for a person, and whether
            # anyone came: the number that says if this design is usable.
            telemetry.histogram("xsm.worker.approval_wait.duration", time.time() - start,
                                {"xsm.approval.outcome": "approved" if allowed else "denied"})


def _await_person_inner(worker: dict, runtime: str, tool: str, tool_input) -> tuple:
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
            if cur["status"] == "denied":
                _notify_parent(worker, cur, outcome="denied (%s)" % (cur.get("reason") or
                                                                  "your user said no"))
            return cur["status"] == "approved", cur.get("reason")
        time.sleep(1)
    req.update({"status": "denied", "reason": "nobody answered within %ds" %
                (deadline - req["t"])})
    paths.write_json(_approval_path(req["id"]), req)
    _notify_parent(worker, req, outcome=req["reason"])
    return False, req["reason"]


def _decision(allow: bool, reason: str | None) -> dict:
    decision = {"behavior": "allow"} if allow else \
        {"behavior": "deny", "message": "denied through xsm: %s" % (reason or "the user said no")}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def _notify_parent(worker: dict, req: dict, outcome: str | None = None) -> None:
    """Tell the session that started the worker, as a task it acts on now: a
    worker must never sit idle on a permission nobody was asked for."""
    if not worker.get("parent_ref") or not worker.get("session_id"):
        return
    try:
        me = registry.by_session(worker["runtime"], worker["session_id"])
        if not me:
            return
        from . import send as send_mod
        if outcome:
            text = ("Worker %s's request [%s] %s ended: %s. The worker carries on without it. If "
                    "the work needs it, ask your user again with the xsm_approve MCP tool when it "
                    "asks again, or re-send the task with what they allow."
                    % (worker["name"], req["id"], req["summary"], outcome))
            kind = "note"
        else:
            text = ("Worker %s is blocked on a permission and is waiting: [%s] %s. Now, call the "
                    "xsm_approve MCP tool with id %s: it shows the request to your user, who "
                    "allows or denies it. Do not decide it yourself, and do not leave it waiting."
                    % (worker["name"], req["id"], req["summary"], req["id"]))
            kind = "task"
        send_mod.send("ref:%s" % worker["parent_ref"], text, sender=me, kind=kind)
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


def answer_asked(req_id: str, approve: bool, asked_by: str | None, reason: str | None = None) -> dict:
    """An answer the person gave in an MCP elicitation form (xsm_approve). The
    form is the check that a person answered, so no terminal is needed; only
    the session that started the worker may ask."""
    req = paths.read_json(_approval_path(req_id))
    if not req or req.get("status") != "pending":
        raise WorkerError("no pending approval request %s" % req_id)
    worker = load(req.get("worker") or "") or {}
    if worker.get("parent_ref") != asked_by:
        raise WorkerError("request %s belongs to a worker another session started" % req_id)
    req.update({"status": "approved" if approve else "denied", "answered": time.time(),
                "reason": reason, "via": "mcp-elicitation"})
    paths.write_json(_approval_path(req_id), req)
    return req


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
    try:
        from . import telemetry
    except ImportError:
        telemetry = None
    if telemetry:
        telemetry.counter("xsm.worker.stopped", 1,
                          {"xsm.worker.runtime": worker.get("runtime"), "xsm.stop.reason": reason})
        telemetry.histogram("xsm.worker.lifetime", time.time() - (worker.get("created") or
                                                                  time.time()),
                            {"xsm.worker.runtime": worker.get("runtime")})
    if worker.get("pane"):
        subprocess.run(["tmux", "kill-pane", "-t", worker["pane"]], capture_output=True, timeout=5)
    if worker.get("pid") and worker.get("lstart"):
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
            os.killpg(pid, sig)             # a tmux pane's process leads its own group
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


def orphans() -> list:
    """Workers whose starting session is over: it ended, stopped without a
    goodbye, or its pointer is gone (a stopped parent worker's is deleted, so
    this cascades down a tree). A parent whose state is unknown — a Codex pid
    that could not be confirmed — is not over. Also workers whose own process
    is gone, which have nothing left to stop but their records."""
    by_ref = {r.get("ref"): r for r in registry.records()}
    out = []
    for w in all_workers():
        parent = by_ref.get(w.get("parent_ref")) if w.get("parent_ref") else None
        if w.get("parent_ref") and (parent is None or parent.get("state") in ("ended", "stale")):
            out.append((w, "its starting session is over"))
        elif state(w) == "gone" and time.time() - w.get("created", 0) > 120:
            out.append((w, "its process is gone"))
    return out


def reap() -> list:
    stopped = []
    for w, why in orphans():
        try:
            stop(w["name"], reason=why)
            stopped.append((w["name"], why))
        except WorkerError:
            pass
    return stopped


def reap_detached(ending: dict | None = None) -> None:
    """For hooks: reaping waits on signals, so it runs in its own process.

    `ending` is a session saying goodbye. Its SessionEnd hook runs while its
    process is still alive, so it still reads as live; the reaper waits for
    that process to exit before it looks."""
    argv = [install.pinned_python(), "-m", "xsm", "reap"]
    if ending:
        if not any(w.get("parent_ref") == ending.get("ref") for w in all_workers()):
            return
        if ending.get("pid"):
            argv += ["--after-pid", str(ending["pid"])]
    elif not orphans():
        return
    env = dict(os.environ)
    env["PYTHONPATH"] = install.REPO
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=env, start_new_session=True)


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

def attach(name: str, out=sys.stdout) -> int:
    """Go to the worker's own screen. It is a real TUI in tmux, so watching it
    and answering it both happen there; what it is doing at a glance is on the
    statusline."""
    worker = load(name)
    if not worker:
        raise WorkerError("no worker named %s" % name)
    target = worker.get("pane")
    if not target:
        raise WorkerError("%s has no tmux pane on record" % name)
    if worker["mode"] == "pane":
        out.write("%s runs in tmux pane %s: tmux select-pane -t %s\n" % (name, target, target))
        return 0
    verb = "switch-client" if os.environ.get("TMUX") else "attach"
    if human_terminal():
        os.execvp("tmux", ["tmux", verb, "-t", target])
    out.write("%s runs in the background: tmux %s -t %s\n" % (name, verb, target))
    return 0
