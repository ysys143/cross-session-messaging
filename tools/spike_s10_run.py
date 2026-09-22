#!/usr/bin/env python3
"""S10 harness: run N Codex sessions as peers on one shared note graph.

Peers, not workers. `xsm spawn` would make every session a child of the caller —
a tree, which is option G in ADR-0012 and outside what S10 measures. So this
starts each session with `codex exec` and, when a turn ends before time is up,
resumes the same thread (one identity per slot, as agora's launcher did with one
account per worker).

Every turn states its sandbox. `exec resume` takes no --sandbox flag and falls
back to the user's config (measured in xsm/workers.py); here that config is
danger-full-access, so `-c sandbox_mode="workspace-write"` goes on every call.

    tools/spike_s10_run.py --condition 2 --slots 3 --minutes 30 --out <dir>

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
MODEL = "gpt-5.6-sol"
EFFORT = "medium"

PEERS = ("You are one of several agents working on this problem at the same time, in this "
         "folder, on the same shared record. ")
CHANNEL = ("\n## Other agents\n\n`./xsm list` shows the other sessions here. `./xsm send <name> "
           "--text ...` messages one; `./xsm post --text ...` writes to this folder's channel "
           "and `./xsm channel` reads it.\n")
RULE_WIP = ("\n## Before you start an experiment\n\nAdd a `wip` node whose parent is the node "
            "you are about to build on (`--tag wip --text \"<what you will try>\"`). Do not "
            "build on a node that already has a `wip` child from someone else.\n")
RULE_CHANNEL = ("When you add the `wip` node, also `./xsm post --tag note --text \"taking <id>: "
                "<what>\"` so the others see it on the channel.\n")
RESUME = ("Continue the loop in the brief: read the record, pick a parent, one change, "
          "evaluate, publish. Stop only when ./timeleft prints 0.")


def brief(condition: int) -> str:
    with open(os.path.join(TASK, "brief.md")) as fh:
        text = fh.read()
    shared = condition >= 2
    rules = (RULE_WIP if condition >= 3 else "") + (RULE_CHANNEL if condition >= 4 else "")
    return (text.replace("{PEERS}", PEERS if shared else "")
                .replace("{CHANNEL}", CHANNEL if shared else "")
                .replace("{RULES}", rules))


def workspace(root: str, deadline: float) -> str:
    """A fresh folder: task files, the seed graph, and two helper scripts."""
    os.makedirs(os.path.join(root, "candidates"), exist_ok=True)
    for name in ("eval.py", "answers.json", "lcs_baseline.py"):
        shutil.copy(os.path.join(TASK, name), root)
    home = os.path.join(root, ".xsm")
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(root, "xsm"), "w") as fh:
        fh.write("#!/bin/sh\nexport XSM_HOME=%s\nexport PYTHONDONTWRITEBYTECODE=1\n"
                 "exec %s \"$@\"\n" % (home, os.path.join(REPO, "bin", "xsm")))
    with open(os.path.join(root, "timeleft"), "w") as fh:
        fh.write("#!/bin/sh\nexec python3 -c 'import time; print(max(0, int(%d - time.time())))'\n"
                 % deadline)
    for script in ("xsm", "timeleft"):
        os.chmod(os.path.join(root, script), 0o755)
    subprocess.run([sys.executable, os.path.join(TASK, "seed.py"), os.path.join(root, "notes.md")],
                   check=True, env=dict(os.environ, XSM_HOME=home, PYTHONDONTWRITEBYTECODE="1"))
    return home


def codex_args(root: str, thread: str | None, prompt: str) -> list:
    common = ["--json", "--skip-git-repo-check", "-m", MODEL,
              "-c", 'model_reasoning_effort="%s"' % EFFORT,
              "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"']
    if thread:
        return ["codex", "exec", "resume"] + common + [thread, prompt]
    return ["codex", "exec", "-C", root] + common + [prompt]


def watch(root: str, deadline: float, dest: str) -> None:
    """Keep every version of every candidate file, outside the agents' folder.

    Sessions share one candidates/ folder and do pick the same filename (the
    pilot saw it within minutes), so the file a node names may by now hold
    someone else's code. The duplicate count compares code changes, and needs
    the version that existed when each node was published.
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
    """Ship this run's xsm telemetry to an OTLP collector while it runs, so a
    person can watch the sessions' commands — refusals included — as they
    happen instead of reading JSONL afterwards."""
    return subprocess.Popen(
        [os.path.join(REPO, "bin", "xsm"), "otlp-export", "--follow", "--interval", "5",
         "--endpoint", endpoint],
        env=dict(os.environ, XSM_HOME=home, PYTHONDONTWRITEBYTECODE="1"),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _thread_of(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "thread.started":
            return ev.get("thread_id")
    return None


def slot(n: int, root: str, home: str, condition: int, deadline: float, log_dir: str) -> None:
    env = dict(os.environ, XSM_HOME=home, PYTHONDONTWRITEBYTECODE="1")
    for k in [k for k in env if k.startswith(("CLAUDE_", "ORCA_", "CODEX_COMPANION"))]:
        del env[k]                          # this harness runs inside a Claude session
    thread, turns = None, 0
    with open(os.path.join(log_dir, "slot%d.jsonl" % n), "a") as log:
        while time.time() < deadline - 20:
            prompt = brief(condition) if thread is None else RESUME
            turns += 1
            log.write(json.dumps({"harness": "turn", "n": turns, "t": time.time()}) + "\n")
            log.flush()
            try:
                proc = subprocess.run(codex_args(root, thread, prompt), cwd=root, env=env,
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      timeout=max(30, deadline - time.time()))
            except subprocess.TimeoutExpired as exc:
                # The expected ending: the agent kept working until the deadline.
                partial = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                log.write(partial)
                thread = thread or _thread_of(partial)
                log.write(json.dumps({"harness": "timeout", "t": time.time()}) + "\n")
                break
            log.write(proc.stdout)
            if proc.returncode != 0:
                log.write(json.dumps({"harness": "exit", "code": proc.returncode,
                                      "stderr": proc.stderr[-2000:]}) + "\n")
            thread = thread or _thread_of(proc.stdout)
            if not thread:
                log.write(json.dumps({"harness": "no-thread", "stderr": proc.stderr[-2000:]}) + "\n")
                break
            log.flush()
    with open(os.path.join(log_dir, "slot%d.done" % n), "w") as fh:
        json.dump({"thread": thread, "turns": turns, "ended": time.time()}, fh)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--condition", type=int, choices=[1, 2, 3, 4], required=True)
    parser.add_argument("--slots", type=int, default=3)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--out", required=True)
    parser.add_argument("--otlp", help="OTLP/HTTP endpoint to ship xsm telemetry to while running")
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=False)
    deadline = time.time() + args.minutes * 60
    roots = ([os.path.join(out, "slot%d" % i) for i in range(args.slots)] if args.condition == 1
             else [os.path.join(out, "shared")] * args.slots)
    homes = {}
    for root in dict.fromkeys(roots):
        homes[root] = workspace(root, deadline)
    with open(os.path.join(out, "run.json"), "w") as fh:
        json.dump({"condition": args.condition, "slots": args.slots, "minutes": args.minutes,
                   "model": MODEL, "effort": EFFORT, "started": time.time(),
                   "deadline": deadline, "roots": sorted(set(roots))}, fh, indent=1)
    watchers = [threading.Thread(target=watch, daemon=True,
                                 args=(root, deadline,
                                       os.path.join(out, "history", os.path.basename(root))))
                for root in dict.fromkeys(roots)]
    for w in watchers:
        w.start()
    shipping = [exporter(home, args.otlp) for home in homes.values()] if args.otlp else []
    threads = [threading.Thread(target=slot, args=(i, roots[i], homes[roots[i]], args.condition,
                                                   deadline, out))
               for i in range(args.slots)]
    for t in threads:
        t.start()
        time.sleep(2)                       # distinct start times, like staggered launches
    for t in threads:
        t.join()
    for w in watchers:
        w.join(timeout=40)
    for proc, home in zip(shipping, homes.values()):
        proc.terminate()
        proc.wait(timeout=10)
        subprocess.run([os.path.join(REPO, "bin", "xsm"), "otlp-export", "--once",
                        "--endpoint", args.otlp],
                       env=dict(os.environ, XSM_HOME=home, PYTHONDONTWRITEBYTECODE="1"),
                       capture_output=True, timeout=60)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
