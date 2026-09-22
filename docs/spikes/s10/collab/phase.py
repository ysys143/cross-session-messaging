#!/usr/bin/env python3
"""Print the current phase of the collab scenario and the time left in it.

    python3 phase.py <start-epoch> <agree> <analyse> <discuss> <revise>   (minutes each)
"""
import sys
import time

NAMES = ("1/4 합의 (agree who looks at what)", "2/4 분석 (analyse your part; do not edit yet)",
         "3/4 논의 (share findings, agree on changes)", "4/4 수정 (revise ADR-DRAFT.md together)")


def main() -> int:
    start = float(sys.argv[1])
    lengths = [float(m) * 60 for m in sys.argv[2:6]]
    elapsed = time.time() - start
    if elapsed < 0:
        print("not started yet; starts in %ds" % -elapsed)
        return 0
    for name, length in zip(NAMES, lengths):
        if elapsed < length:
            print("phase %s — %ds left in this phase" % (name, length - elapsed))
            return 0
        elapsed -= length
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
