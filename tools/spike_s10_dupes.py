#!/usr/bin/env python3
"""Count duplicate work in one S10 run (docs/spikes/S10-swarm-duplication.md §2).

A duplicate pair is two nodes from different sessions, with the same tags,
that did the same work. "The same work" is measured two ways, because the
pilot showed prose alone cannot tell it:

- prose: Jaccard over the content words of the two descriptions, with the
  boilerplate every node repeats ("ok=true", "Correct on all 30 pairs",
  "Created candidates/…") and all numbers removed.
- code: Jaccard over the tokens of the lines each node *added* to its parent's
  code. Two sessions making the same change describe it differently but write
  much the same lines. Needs the harness's history/ snapshots; without them
  only prose is reported.

Parents are not required to match. In the pilot most real duplicates were the
same step taken on each session's own copy of an earlier step — different
parent ids, same work — and agora's 696 pairs were counted without regard to
parents either. Whether a pair shares its parents is kept as a flag.

Seed, setup, wip and verification nodes are excluded: seed and setup are
given, wip is a claim, and a verification is a reproduction, not waste.

Overwrites are counted separately: one candidate file that result nodes from
two or more sessions each name as the file they created.

    tools/spike_s10_dupes.py <run-dir> [--json] [--label-sheet out.md]
"""
import argparse
import datetime
import difflib
import glob
import itertools
import json
import os
import random
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from xsm import doc  # noqa: E402

THRESHOLDS = (0.2, 0.3, 0.4, 0.5, 0.6)
EXCLUDED = {"setup", "wip", "verification"}
FIXED_FILES = ("eval.py", "baseline.py")
BOILERPLATE = set("""
ok true false correct correctly pairs pair all fixed created create wrote candidate candidates
change changed changes one single from parent parents node eval evaluated evaluation measured
measure result results faster slower than same tied improving improved improvement vs versus
the and with for was were that this into then only still also now using use used runs run
python file files best time times total output check checks passed passes
""".split())
FILE_RE = re.compile(r"([\w.-]+\.py)\b")
CODE_TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\d+|<<|>>|[-+*/&|^~%<>=!]=?")


def words(text: str) -> set:
    text = FILE_RE.sub(" ", text.lower())
    return {t for t in re.split(r"[^a-z0-9가-힣]+", text) if len(t) > (1 if re.match("[가-힣]", t) else 2) and t not in BOILERPLATE}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def who(node: dict) -> str:
    """The session behind a node: its ref, not its author string. A Claude
    session's auto name can change mid-run (seen in the first mixed run:
    one ref under two names), and the name is part of the string."""
    m = re.search(r"\[([0-9a-f]{6})\]\s*$", node["author"])
    return m.group(1) if m else node["author"]


def when(node: dict) -> float:
    return datetime.datetime.strptime(node["t"], "%Y-%m-%dT%H:%M:%S%z").timestamp()


def primary_file(node: dict) -> str | None:
    """The file a node reports on: the first .py it names that is not given."""
    for name in FILE_RE.findall(node["body"]):
        if name not in FIXED_FILES:
            return name
    return None


def notes_dirs(run: str) -> list:
    """(folder, nodes) for every notes.md in the run: one shared, or one per slot."""
    found = []
    for root, dirs, _files in os.walk(run):
        dirs[:] = [d for d in dirs if not d.endswith(".nodes") and d not in (".xsm", "history")]
        if os.path.isdir(os.path.join(root, "notes.md.nodes")):
            found.append((root, doc.read(os.path.join(root, "notes.md"))))
    return found


class History:
    """Every snapshot the harness took of one folder's candidates/."""

    def __init__(self, run: str, folder: str):
        base = os.path.join(run, "history", os.path.basename(folder))
        self.versions = {}
        for path in glob.glob(os.path.join(base, "*.py@*")):
            name, _, ns = os.path.basename(path).rpartition("@")
            self.versions.setdefault(name, []).append((int(ns) / 1e9, path))
        for v in self.versions.values():
            v.sort()
        with open(os.path.join(REPO, "docs", "spikes", "s10", "task", "baseline.py")) as fh:
            self.baseline = fh.read()

    def code_at(self, name: str | None, t: float) -> str | None:
        """The newest version of `name` written no later than a few seconds
        after t (node timestamps have one-second resolution)."""
        if not name:
            return None
        if name == "baseline.py":
            return self.baseline
        latest = None
        for mtime, path in self.versions.get(name, []):
            if mtime <= t + 5:
                latest = path
        if latest is None:
            return None
        with open(latest) as fh:
            return fh.read()


def _code_lines(code: str) -> list:
    out = []
    for line in code.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith(('"""', "'''")):
            out.append(re.sub(r"\s+", " ", line))
    return out


def added_tokens(node: dict, byid: dict, hist: History | None) -> set | None:
    if hist is None:
        return None
    mine = hist.code_at(primary_file(node), when(node))
    if mine is None:
        return None
    parent = byid.get(node["parents"][0]) if node["parents"] else None
    if parent is None or "seed" in parent["author"]:
        base = hist.baseline
    else:
        base = hist.code_at(primary_file(parent), when(parent)) or ""
    added = [l[2:] for l in difflib.ndiff(_code_lines(base), _code_lines(mine))
             if l.startswith("+ ")]
    return set(CODE_TOKEN_RE.findall(" ".join(added))) or None


def analyse(run: str) -> dict:
    counted, byid, overwrites, code = [], {}, {}, {}
    for folder, nodes in notes_dirs(run):
        hist = History(run, folder) if os.path.isdir(os.path.join(run, "history")) else None
        byid.update({n["id"]: n for n in nodes})
        mine = [n for n in nodes if "seed" not in n["author"] and not EXCLUDED & set(n["tags"])]
        counted += mine
        for n in mine:
            code[n["id"]] = added_tokens(n, byid, hist)
            if "result" in n["tags"] and primary_file(n):
                overwrites.setdefault((folder, primary_file(n)), set()).add(who(n))
    prose = {n["id"]: words(n["body"]) for n in counted}
    pairs = []
    for a, b in itertools.combinations(counted, 2):
        if who(a) == who(b) or sorted(a["tags"]) != sorted(b["tags"]):
            continue
        ca, cb = code.get(a["id"]), code.get(b["id"])
        pairs.append({"a": a["id"], "b": b["id"],
                      "prose": round(jaccard(prose[a["id"]], prose[b["id"]]), 3),
                      "code": round(jaccard(ca, cb), 3) if ca and cb else None,
                      "same_parent": sorted(a["parents"]) == sorted(b["parents"])})
    rates = {}
    for signal in ("prose", "code"):
        rates[signal] = {}
        for cut in THRESHOLDS:
            hit = [p for p in pairs if p[signal] is not None and p[signal] >= cut]
            nodes_in = {x for p in hit for x in (p["a"], p["b"])}
            rates[signal][str(cut)] = {
                "pairs": len(hit), "nodes_in_pairs": len(nodes_in),
                "rate": round(len(nodes_in) / len(counted), 3) if counted else 0.0}
    authors = {}
    for n in byid.values():
        if "seed" not in n["author"]:
            key = "/".join(n["tags"])
            authors.setdefault(who(n), {}).setdefault(key, 0)
            authors[who(n)][key] += 1
    return {"run": run, "nodes_counted": len(counted),
            "code_coverage": round(sum(1 for n in counted if code.get(n["id"])) / len(counted), 3)
            if counted else 0.0,
            "authors": authors,
            "overwrites": sorted(name for (_, name), who in overwrites.items() if len(who) > 1),
            "by_threshold": rates, "pairs": pairs, "_nodes": byid}


def label_sheet(result: dict, path: str, per_signal: int = 12, random_n: int = 8) -> int:
    """Pairs for a person to judge: the top of each signal plus a random draw,
    shuffled so the order does not hint at the score."""
    pairs, nodes = result["pairs"], result["_nodes"]
    chosen = {}
    for signal in ("prose", "code"):
        ranked = sorted((p for p in pairs if p[signal] is not None), key=lambda p: -p[signal])
        for p in ranked[:per_signal]:
            chosen[(p["a"], p["b"])] = p
    rest = [p for p in pairs if (p["a"], p["b"]) not in chosen]
    rng = random.Random(10)
    for p in rng.sample(rest, min(random_n, len(rest))):
        chosen[(p["a"], p["b"])] = p
    rows = list(chosen.values())
    rng.shuffle(rows)
    with open(path, "w") as fh:
        fh.write("# S10 labelling sheet — %s\n\nFor each pair, mark **same** if both nodes did "
                 "the same piece of work (the same change, idea or trick), **diff** otherwise. "
                 "Scores are hidden until the end on purpose.\n\n" % result["run"])
        for i, p in enumerate(rows, 1):
            a, b = nodes[p["a"]], nodes[p["b"]]
            fh.write("## %d.  [ ] same  [ ] diff\n\n- A `%s`: %s\n- B `%s`: %s\n\n" % (
                i, p["a"], a["body"].replace("\n", " ")[:400],
                p["b"], b["body"].replace("\n", " ")[:400]))
        fh.write("---\n\n## Scores (read after labelling)\n\n| # | prose | code | same parent |\n"
                 "|---|---|---|---|\n")
        for i, p in enumerate(rows, 1):
            fh.write("| %d | %s | %s | %s |\n" % (i, p["prose"], p["code"], p["same_parent"]))
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--label-sheet")
    args = parser.parse_args()
    result = analyse(os.path.abspath(args.run))
    if args.label_sheet:
        print("wrote %d pairs to %s" % (label_sheet(result, args.label_sheet), args.label_sheet))
    result.pop("_nodes")
    if args.json:
        print(json.dumps(result, indent=1))
        return 0
    print("%d nodes counted, %d authors, code coverage %.0f%%" % (
        result["nodes_counted"], len(result["authors"]), 100 * result["code_coverage"]))
    for signal, rows in result["by_threshold"].items():
        print("  %s" % signal)
        for cut, row in rows.items():
            print("    >= %s  %3d pair(s)  %3d node(s)  rate %.3f" % (
                cut, row["pairs"], row["nodes_in_pairs"], row["rate"]))
    print("  overwrites: %d %s" % (len(result["overwrites"]), ", ".join(result["overwrites"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
