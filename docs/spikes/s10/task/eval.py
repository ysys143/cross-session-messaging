#!/usr/bin/env python3
"""Score a candidate lcs_len: correctness on 30 fixed pairs, then best-of-3 time.

    python3 eval.py candidates/mine.py   ->   {"ok": true, "ms": 812.4}

The pairs are generated from a fixed seed and the answers are stored in
answers.json, so every agent is measured on exactly the same input.
"""
import importlib.util
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def pairs() -> list:
    rng = random.Random(42)
    out = []
    for i in range(30):
        n = rng.randint(200, 600)
        a = "".join(rng.choice("ACGT") for _ in range(n))
        if i % 3 == 0:
            # A mutated copy: long shared runs, the case prefix/suffix tricks target.
            b = "".join(c if rng.random() > 0.1 else rng.choice("ACGT") for c in a)
        else:
            b = "".join(rng.choice("ACGT") for _ in range(rng.randint(200, 600)))
        out.append((a, b))
    return out


def load(path: str):
    spec = importlib.util.spec_from_file_location("candidate", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s as a Python module" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.lcs_len


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 eval.py <candidate.py>", file=sys.stderr)
        return 2
    with open(os.path.join(HERE, "answers.json")) as fh:
        answers = json.load(fh)
    fn = load(sys.argv[1])
    data = pairs()
    got = [fn(a, b) for a, b in data]
    wrong = [i for i, (g, want) in enumerate(zip(got, answers)) if g != want]
    if wrong:
        print(json.dumps({"ok": False, "wrong_pairs": wrong[:5]}))
        return 1
    best = float("inf")
    for _ in range(3):
        # A fresh module each time: the correctness pass above would otherwise
        # have filled any in-memory cache, and "fast" would mean "remembered".
        # (S10 pilot, 2026-09-22: two sessions found lru_cache and reported 0.0 ms.)
        fn = load(sys.argv[1])
        start = time.perf_counter()
        for a, b in data:
            fn(a, b)
        took = (time.perf_counter() - start) * 1000
        best = took if best is None else min(best, took)
    print(json.dumps({"ok": True, "ms": round(best, 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
