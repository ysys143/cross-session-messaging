#!/usr/bin/env python3
"""Write the S10 seed graph — five nodes, the same in every run.

    python3 seed.py <run-folder>/notes.md
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))))
from xsm import doc  # noqa: E402

SEED = {"kind": "agent", "name": "seed", "alias": "s10", "ref": "seed00"}


def main() -> int:
    notes = sys.argv[1]
    setup = doc.add(notes, SEED, "Task: make lcs_len fast. Evaluator: python3 eval.py <file>. "
                    "Baseline: lcs_baseline.py (full 2-D table).", ["setup"])
    base = doc.add(notes, SEED, "Baseline lcs_baseline.py as given: ok, 442 ms on the "
                   "reference machine. Full (n+1)x(m+1) list-of-lists table.", ["result"],
                   [setup["id"]])
    memo = doc.add(notes, SEED, "Tried top-down recursion with functools.lru_cache: hits the "
                   "recursion limit on 600-character inputs and is slower where it runs. "
                   "Negative result.", ["result"], [base["id"]])
    doc.add(notes, SEED, "Only the previous row of the table is ever read, so two rows "
            "should be enough; less allocation might also be faster.", ["hypothesis"],
            [base["id"]])
    doc.add(notes, SEED, "A third of the pairs are near-copies of each other; a shared prefix "
            "and suffix could be counted directly and cut out of the table.", ["hypothesis"],
            [base["id"], memo["id"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
