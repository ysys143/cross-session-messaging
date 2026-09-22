#!/usr/bin/env python3
"""One statusline line for the S10 run in progress, or nothing.

With --compose (how this repo's .claude/settings.local.json runs it), the
statusline that would apply without that file — this project's settings,
else the user's — runs first with the same input and prints unchanged, and
the S10 line goes under it: a dashboard, Orca's line, anything, keeps its
place (user decision, 2026-09-22).

    S10 collab1 | 2/4 분석 4:12 | luna:busy haiku:idle sonnet:busy | msg 3 ch 1 doc v2

Picks the newest .local/s10/*/run.json whose deadline is less than two
minutes past. Cheap enough to run every few seconds: directory listings, and
one `tmux capture-pane` per worker to tell working from waiting.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUSY = re.compile(r"esc to interrupt|Working \(|… ?\(\d")


def _count(pattern: str) -> int:
    return len(glob.glob(pattern))


def _lines(pattern: str) -> int:
    n = 0
    for p in glob.glob(pattern):
        try:
            with open(p, "rb") as fh:
                n += sum(1 for _ in fh)
        except OSError:
            pass
    return n


def _state(pane: str) -> str:
    try:
        screen = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane], capture_output=True,
                                text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return "?"
    if screen.returncode != 0:
        return "gone"
    text = screen.stdout
    if "Do you want to proceed" in text or "trust this folder" in text \
            or "trust the contents" in text:
        return "ASKS"
    return "busy" if BUSY.search(text) else "idle"


def _base_command() -> str | None:
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    for settings in (os.path.join(REPO, ".claude", "settings.json"),
                     os.path.join(home, "settings.json")):
        try:
            with open(settings) as fh:
                command = ((json.load(fh) or {}).get("statusLine") or {}).get("command")
        except (OSError, ValueError):
            continue
        if command and "s10_status" not in command:
            return command
    return None


def compose() -> None:
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    command = _base_command()
    if command:
        try:
            out = subprocess.run(["/bin/sh", "-c", command], input=raw, capture_output=True,
                                 text=True, timeout=5).stdout
            if out.strip():
                print(out.rstrip("\n"))
        except (OSError, subprocess.SubprocessError):
            pass


def main() -> int:
    if "--compose" in sys.argv:
        compose()
    now = time.time()
    runs = []
    for p in glob.glob(os.path.join(REPO, ".local", "s10", "*", "run.json")):
        try:
            with open(p) as fh:
                run = json.load(fh)
        except (OSError, ValueError):
            continue
        if run.get("deadline", 0) + 120 > now:
            runs.append((run.get("started", 0), p, run))
    if not runs:
        return 0
    _, path, run = max(runs)
    out = os.path.dirname(path)
    root = run["roots"][0]
    phase = ""
    if run.get("scenario") == "collab" and os.path.exists(os.path.join(root, "phase")):
        try:
            said = subprocess.run([os.path.join(root, "phase")], capture_output=True, text=True,
                                  timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            said = ""
        m = re.match(r"phase (\d/4) (\S+).*?(\d+)s left", said)
        phase = "%s %s %d:%02d" % (m.group(1), m.group(2), int(m.group(3)) // 60,
                                   int(m.group(3)) % 60) if m else said[:12]
    else:
        left = max(0, int(run["deadline"] - now))
        phase = "%d:%02d left" % (left // 60, left % 60)
    workers = " ".join("%s:%s" % (w["name"].split("-")[1], _state(w["pane"]) if w.get("pane")
                                  else "FAILED")
                       for w in run.get("workers", []))
    home = os.path.join(root, ".xsm")
    counts = "msg %d ch %d" % (_count(os.path.join(home, "ledger", "*.json")),
                               _lines(os.path.join(home, "channels", "*", "*.jsonl")))
    if run.get("scenario") == "collab":
        counts += " doc v%d" % _count(os.path.join(out, "history", "*", "*@*"))
    else:
        counts += " nodes %d" % max(0, _count(os.path.join(root, "notes.md.nodes", "*.md")) - 5)
    print("S10 %s | %s | %s | %s" % (os.path.basename(out), phase, workers, counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
