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
    setup = doc.add(notes, SEED, "Task: compress corpus.txt (81,086 bytes). Score = compressed "
                    "size + source size; evaluator: python3 eval.py <file>.", ["setup"])
    base = doc.add(notes, SEED, "baseline.py stores the corpus as it is: ok, score 81208 "
                   "(81086 compressed + 122 code).", ["result"], [setup["id"]])
    cheat = doc.add(notes, SEED, "Tried embedding the corpus in the decompressor: the source "
                    "counts toward the score, so it cannot beat the baseline. Negative result.",
                    ["result"], [base["id"]])
    doc.add(notes, SEED, "Two thirds of the bytes (54,279) are Hangul, three bytes per "
            "character in UTF-8; a model that sees characters rather than bytes may do better.",
            ["hypothesis"], [base["id"]])
    doc.add(notes, SEED, "The corpus is eight documents with the same section headings and "
            "many repeated phrases; long repeats should be cheap to refer back to.",
            ["hypothesis"], [base["id"], cheat["id"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
