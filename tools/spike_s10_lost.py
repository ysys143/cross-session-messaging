#!/usr/bin/env python3
"""Work thrown away when several sessions edit one file.

In the compress scenario the waste is duplication: two sessions doing the same
piece of work in their own nodes. When they edit one file instead, the waste is
different — one session's text disappears under another's write, and nobody
notices because the file still looks fine.

This counts that from the snapshots the harness keeps (`history/<folder>/<file>@
<mtime-ns>`), with no guess about who wrote what:

- **lost line**: a line that some version had, the next version does not, no later
  version has again, and that the same write did not replace with something close
  to it. A rewrite is not lost work — a line that simply vanished is.
- **churned line**: a line that disappears and comes back later. Someone's write
  landed on top of another's and was restored — the conflict happened, the work
  survived.
- **close write**: two consecutive versions less than `--window` seconds apart
  (default 20). Concurrent editing looks like this; a session that waits for the
  others does not.

A lost line is not always a mistake — an edit that deletes a paragraph on purpose
also loses lines. So the report separates deletions that stand (the final file is
shorter) from lines lost while the file kept growing, and prints the lost lines so
a person can see which they are.
"""
from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import sys


def versions(run: str) -> list:
    """[(epoch, path)] oldest first, for the one file the run tracked. The
    harness has written the stamp as both mtime-ns and epoch seconds."""
    found = []
    for pattern in (os.path.join(run, "history", "*", "*@*"),
                    os.path.join(run, "history", "*@*")):
        for path in glob.glob(pattern):
            stamp = path.rsplit("@", 1)[1]
            if stamp.isdigit():
                found.append((int(stamp) / 1e9 if len(stamp) > 12 else int(stamp), path))
    return sorted(found)


def _replaced(line: str, added: list, ratio: float = 0.6) -> bool:
    """Whether this write put something close to `line` in its place."""
    return bool(difflib.get_close_matches(line, added, n=1, cutoff=ratio))


def _lines(path: str) -> list:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [line.rstrip("\n") for line in fh]


def measure(run: str, window: float = 20.0) -> dict:
    steps = versions(run)
    if len(steps) < 2:
        return {"versions": len(steps), "error": "nothing to compare"}
    seen = [(when, _lines(path)) for when, path in steps]
    final = set(seen[-1][1])
    lost, churned, close = [], [], 0
    rewritten = 0
    for i in range(len(seen) - 1):
        before, after = set(seen[i][1]), set(seen[i + 1][1])
        if seen[i + 1][0] - seen[i][0] < window:
            close += 1
        added = [line for line in after - before if line.strip()]
        for line in before - after:
            if not line.strip():
                continue
            if line in final:
                continue                      # still there at the end
            if _replaced(line, added):
                rewritten += 1                # the same write put a new version in
                continue
            if any(line in later for _when, later in seen[i + 2:]):
                churned.append(line)          # came back after being overwritten
            else:
                lost.append(line)
    grew = len(seen[-1][1]) > len(seen[0][1])
    return {
        "versions": len(seen),
        "span_s": round(seen[-1][0] - seen[0][0], 1),
        "close_writes": close,
        "lost_lines": len(lost),
        "rewritten_lines": rewritten,
        "churned_lines": len(churned),
        "final_lines": len(seen[-1][1]),
        "first_lines": len(seen[0][1]),
        "file_grew": grew,
        "lost": lost[:40],
        "churned": churned[:20],
    }


def report(run: str, data: dict) -> str:
    if data.get("error"):
        return "%s: %s (%d version(s))" % (run, data["error"], data["versions"])
    out = ["%s: %d versions over %ss, %d written less than the window apart" % (
        run, data["versions"], data["span_s"], data["close_writes"])]
    out.append("  lost %d line(s), churned %d, rewritten %d, file %d -> %d lines" % (
        data["lost_lines"], data["churned_lines"], data["rewritten_lines"],
        data["first_lines"], data["final_lines"]))
    for line in data["lost"][:10]:
        out.append("    lost: %s" % line[:110])
    for line in data["churned"][:5]:
        out.append("    churned: %s" % line[:110])
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--window", type=float, default=20.0,
                        help="seconds under which two writes count as concurrent")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = {run: measure(run, args.window) for run in args.runs}
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return 0
    for run, data in results.items():
        print(report(run, data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
