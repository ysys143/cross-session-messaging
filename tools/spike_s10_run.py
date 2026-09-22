#!/usr/bin/env python3
"""S10 harness: live sessions working on one shared note graph.

Every session is a real, interactive TUI started by `xsm spawn --background`:
a window of the detached tmux session xsm-workers, registered with xsm, able to
receive a message at any time. Never `claude -p` or `codex exec` — those run
one prompt and are gone, so there would be no session for xsm to connect and
nothing for this spike to measure (user decision, 2026-09-22; every pilot run
before this harness was void for that reason, see S10 §6-1).

The harness owns the lifecycle — it starts the workers, gives each the brief
the way a person would, and stops them at the deadline — and nothing else. It
does not assign work and does not nudge a session that has gone quiet: what
the sessions do with each other is what is measured.

Each run has its own folder and its own XSM_HOME, so runs never see each
other. The workers have no parent session (none is registered in a run's
store); xsm reaps a parentless worker only once its own process is gone, so
none is stopped mid-run. Put runs inside a folder every Claude and Codex home
here already trusts, such as .local/s10/ in this repo: a folder-trust screen is
a question for a person, the harness never answers one, and a worker that
stops at one fails its slot and says so.

    tools/spike_s10_run.py --condition 2 --minutes 15 --out .local/s10/run1
    tools/spike_s10_run.py --scenario collab --minutes 15 --out .local/s10/collab1

Scenario `collab` (user request, 2026-09-22): the three sessions review one
document together and revise it — agree who looks at what, analyse, meet and
discuss, then edit the one shared file. Phases run by the clock alone
(`./phase`); nobody announces them, so a session that has gone quiet moves on
only if another session's message wakes it.

Conditions (docs/spikes/S10-swarm-duplication.md §3):
  1 isolated: each slot its own folder and XSM_HOME
  2 flat log: one folder, one XSM_HOME, messaging available, no rule
  3 wip:      2 + "add a wip node before you extend, avoid nodes that have one"
  4 channel:  3 + "also post which node you took to the channel"
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK = os.path.join(REPO, "docs", "spikes", "s10", "task")
COLLAB = os.path.join(REPO, "docs", "spikes", "s10", "collab")
COLLAB_FILES = ("ADR-DRAFT.md", "ref-agora-note.md", "ref-s10-spike.md", "ref-adr-0003.md")
COLLAB_DOC = "ADR-DRAFT.md"
COLLAB_SHARES = (0.15, 0.40, 0.20, 0.25)    # agree, analyse, discuss, revise
DEFAULT_AGENTS = "codex:gpt-5.6-luna,claude:haiku,claude:sonnet"
FIRST_PROMPT = "Read BRIEF.md in this folder and do what it says."
SCREEN_EVERY = 30               # seconds between screen snapshots of each worker

PEERS = ("You are one of several agents working on this problem at the same time, in this "
         "folder, on the same shared record. ")
CHANNEL = ("\n## Other agents\n\n`./xsm list` shows the other sessions here. `./xsm send <name> "
           "--text ...` messages one; `./xsm post --text ...` writes to this folder's channel "
           "and `./xsm channel` reads it. Messages from other agents arrive in this session "
           "as they are sent.\n")
RULE_WIP = ("\n## Before you start an experiment\n\nAdd a `wip` node whose parent is the node "
            "you are about to build on (`--tag wip --text \"<what you will try>\"`). Do not "
            "build on a node that already has a `wip` child from someone else.\n")
RULE_CHANNEL = ("When you add the `wip` node, also `./xsm post --tag note --text \"taking <id>: "
                "<what>\"` so the others see it on the channel.\n")
# spawn refuses inside Orca or herdr; this harness runs its own workers anyway
# (user decision, 2026-09-22), so it hides their pane markers from xsm.
FRAMEWORK_VARS = ("ORCA_TERMINAL_HANDLE", "ORCA_PANE_KEY", "HERDR_PANE_ID", "HERDR_ENV")


def brief(condition: int) -> str:
    with open(os.path.join(TASK, "brief.md")) as fh:
        text = fh.read()
    shared = condition >= 2
    rules = (RULE_WIP if condition >= 3 else "") + (RULE_CHANNEL if condition >= 4 else "")
    return (text.replace("{PEERS}", PEERS if shared else "")
                .replace("{CHANNEL}", CHANNEL if shared else "")
                .replace("{RULES}", rules))


def _write_timeleft(root: str, deadline: float) -> None:
    path = os.path.join(root, "timeleft")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\nexec python3 -c 'import time; print(max(0, int(%d - time.time())))'\n"
                 % deadline)
    os.chmod(path, 0o755)


def _write_phase(root: str, start: float, minutes: float) -> None:
    path = os.path.join(root, "phase")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\nexec python3 %s %d %s\n" % (
            os.path.join(COLLAB, "phase.py"), start,
            " ".join("%.2f" % (minutes * share) for share in COLLAB_SHARES)))
    os.chmod(path, 0o755)


def workspace(root: str, deadline: float, condition: int, scenario: str = "compress") -> str:
    """A fresh folder: task files, the brief, helper scripts, and for the
    compression task the seed graph."""
    if scenario == "collab":
        os.makedirs(root, exist_ok=True)
        for name in COLLAB_FILES:
            shutil.copy(os.path.join(COLLAB, name), root)
        shutil.copy(os.path.join(COLLAB, "brief.md"), os.path.join(root, "BRIEF.md"))
        _write_phase(root, deadline, 1)     # "not started yet" until the real clock is set
    else:
        os.makedirs(os.path.join(root, "candidates"), exist_ok=True)
        for name in ("eval.py", "corpus.txt", "baseline.py"):
            shutil.copy(os.path.join(TASK, name), root)
        with open(os.path.join(root, "BRIEF.md"), "w") as fh:
            fh.write(brief(condition))
    home = os.path.join(root, ".xsm")
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(root, "xsm"), "w") as fh:
        fh.write("#!/bin/sh\nexport XSM_HOME=%s\nexport PYTHONDONTWRITEBYTECODE=1\n"
                 "exec %s \"$@\"\n" % (home, os.path.join(REPO, "bin", "xsm")))
    os.chmod(os.path.join(root, "xsm"), 0o755)
    _write_timeleft(root, deadline)
    if scenario != "collab":
        subprocess.run([sys.executable, os.path.join(TASK, "seed.py"),
                        os.path.join(root, "notes.md")],
                       check=True, env=dict(os.environ, XSM_HOME=home, PYTHONDONTWRITEBYTECODE="1"))
    return home


def _env(home: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in FRAMEWORK_VARS and not k.startswith(("CLAUDE_CODE_", "CODEX_COMPANION"))}
    env.pop("CLAUDE_CONFIG_DIR", None)      # the workers use the default homes
    env.update(XSM_HOME=home, PYTHONDONTWRITEBYTECODE="1")
    return env


def _record(home: str, name: str) -> dict:
    try:
        with open(os.path.join(home, "workers", name + ".json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _screen(pane: str) -> str:
    try:
        return subprocess.run(["tmux", "capture-pane", "-p", "-t", pane], capture_output=True,
                              text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def start(runtime: str, model: str, name: str, root: str, home: str, log_dir: str) -> dict:
    """Spawn one live worker and give it the brief. Returns its record, or
    {"name", "error"}: a failed start is reported, never replaced with another
    kind of session."""
    out = subprocess.run([os.path.join(REPO, "bin", "xsm"), "spawn", runtime, "--background",
                          "--name", name, "--model", model, "--dir", root, "--wait", "90"],
                         env=_env(home), cwd=root, capture_output=True, text=True, timeout=150)
    with open(os.path.join(log_dir, "%s.spawn.log" % name), "w") as fh:
        fh.write("exit %d\n%s%s" % (out.returncode, out.stdout, out.stderr))
    rec = _record(home, name)
    if rec.get("waiting"):
        return {"name": name, "error": "stopped at a folder-trust screen; that is a person's "
                                       "question (request %s)" % rec["waiting"]}
    if out.returncode != 0 or not rec.get("ref"):
        lines = (out.stderr or out.stdout).strip().splitlines()
        return {"name": name, "error": lines[-1] if lines else "no output"}
    # What a person would type as the first prompt. One line: a newline typed
    # into the TUI would submit what came before it.
    subprocess.run(["tmux", "send-keys", "-t", rec["pane"], "-l", FIRST_PROMPT], timeout=5)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", rec["pane"], "Enter"], timeout=5)
    rec["briefed"] = time.time()
    return rec


def watch_screens(workers_: list, deadline: float, log_dir: str) -> None:
    """A dated snapshot of every worker's screen: when a session sat idle, and
    what it showed when something went wrong, without anyone attaching."""
    while time.time() < deadline + 5:
        for w in workers_:
            with open(os.path.join(log_dir, "%s.screens.log" % w["name"]), "a") as fh:
                screen = [l for l in _screen(w["pane"]).splitlines() if l.strip()]
                fh.write("===== %s\n%s\n" % (time.strftime("%H:%M:%S"), "\n".join(screen)))
        time.sleep(SCREEN_EVERY)


def watch_file(path: str, deadline: float, dest: str) -> None:
    """Every version of one file, with its time: who overwrote whom in a
    shared document is read off these."""
    os.makedirs(dest, exist_ok=True)
    last = None
    while time.time() < deadline + 30:
        try:
            st = os.stat(path)
        except OSError:
            st = None
        if st and (st.st_mtime_ns, st.st_size) != last:
            last = (st.st_mtime_ns, st.st_size)
            try:
                shutil.copy2(path, os.path.join(dest, "%s@%d" % (os.path.basename(path),
                                                                st.st_mtime_ns)))
            except OSError:
                last = None
        time.sleep(1)


def watch_candidates(root: str, deadline: float, dest: str) -> None:
    """Keep every version of every candidate file, outside the agents' folder.

    Sessions share one candidates/ folder and do pick the same filename, so
    the file a node names may by now hold someone else's code. The duplicate
    count compares code changes, and needs the version that existed when each
    node was published.
    """
    os.makedirs(dest, exist_ok=True)
    seen = set()
    folder = os.path.join(root, "candidates")
    while time.time() < deadline + 30:
        try:
            names = [n for n in os.listdir(folder) if n.endswith(".py")]
        except OSError:
            names = []
        for name in names:
            src = os.path.join(folder, name)
            try:
                st = os.stat(src)
            except OSError:
                continue
            key = (name, st.st_mtime_ns, st.st_size)
            if key in seen:
                continue
            seen.add(key)
            try:
                shutil.copy2(src, os.path.join(dest, "%s@%d" % (name, st.st_mtime_ns)))
            except OSError:
                seen.discard(key)
        time.sleep(2)


def exporter(home: str, endpoint: str) -> subprocess.Popen:
    """Ship this run's xsm telemetry to an OTLP collector while it runs."""
    return subprocess.Popen(
        [os.path.join(REPO, "bin", "xsm"), "otlp-export", "--follow", "--interval", "5",
         "--endpoint", endpoint],
        env=dict(os.environ, XSM_HOME=home, PYTHONDONTWRITEBYTECODE="1"),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(name: str, home: str) -> None:
    subprocess.run([os.path.join(REPO, "bin", "xsm"), "stop", name], env=_env(home),
                   capture_output=True, timeout=60)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=["compress", "collab"], default="compress")
    parser.add_argument("--condition", type=int, choices=[1, 2, 3, 4], default=2,
                        help="compress only; collab is always one shared folder")
    parser.add_argument("--agents", default=DEFAULT_AGENTS,
                        help="one runtime:model per slot, comma-separated (default: %(default)s)")
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument("--out", required=True, help="a new folder, inside a trusted one")
    parser.add_argument("--otlp", help="OTLP/HTTP endpoint to ship xsm telemetry to while running")
    args = parser.parse_args()
    if not shutil.which("tmux"):
        print("tmux is required: the workers are real sessions in tmux", file=sys.stderr)
        return 2
    agents = [a.split(":", 1) for a in args.agents.split(",")]
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=False)
    run = os.path.basename(out)
    roots = ([os.path.join(out, "slot%d" % i) for i in range(len(agents))]
             if args.condition == 1 and args.scenario == "compress"
             else [os.path.join(out, "shared")] * len(agents))
    # Until everyone is up, ./timeleft shows the boot allowance; the real
    # clock is set once the last worker has its brief.
    homes = {root: workspace(root, time.time() + 900, args.condition, args.scenario)
             for root in dict.fromkeys(roots)}

    started = [{}] * len(agents)

    def launch(i):
        runtime, model = agents[i]
        name = "%s-%s-%d" % (run, model.split("-")[-1], i)
        started[i] = start(runtime, model, name, roots[i], homes[roots[i]], out)
        started[i]["root"] = roots[i]

    threads = [threading.Thread(target=launch, args=(i,)) for i in range(len(agents))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    clock = time.time()
    deadline = clock + args.minutes * 60
    for root in homes:
        _write_timeleft(root, deadline)
        if args.scenario == "collab":
            _write_phase(root, clock, args.minutes)
    ready = [w for w in started if not w.get("error")]
    with open(os.path.join(out, "run.json"), "w") as fh:
        json.dump({"scenario": args.scenario, "condition": args.condition,
                   "agents": args.agents, "minutes": args.minutes,
                   "started": time.time(), "deadline": deadline, "roots": sorted(homes),
                   "workers": started}, fh, indent=1)
    for w in started:
        if w.get("error"):
            print("slot %s failed to start: %s" % (w["name"], w["error"]), file=sys.stderr)
    if not ready:
        return 1

    for root in homes:
        dest = os.path.join(out, "history", os.path.basename(root))
        if args.scenario == "collab":
            threading.Thread(target=watch_file, daemon=True,
                             args=(os.path.join(root, COLLAB_DOC), deadline, dest)).start()
        else:
            threading.Thread(target=watch_candidates, daemon=True,
                             args=(root, deadline, dest)).start()
    threading.Thread(target=watch_screens, daemon=True, args=(ready, deadline, out)).start()
    shipping = [(exporter(home, args.otlp), home) for home in homes.values()] if args.otlp else []

    while time.time() < deadline:
        time.sleep(5)
    for w in ready:
        stop(w["name"], homes[w["root"]])
    for proc, home in shipping:
        proc.terminate()
        proc.wait(timeout=10)
        subprocess.run([os.path.join(REPO, "bin", "xsm"), "otlp-export", "--once",
                        "--endpoint", args.otlp], env=dict(os.environ, XSM_HOME=home),
                       capture_output=True, timeout=60)
    print(out)
    return 0 if len(ready) == len(agents) else 1


if __name__ == "__main__":
    sys.exit(main())
