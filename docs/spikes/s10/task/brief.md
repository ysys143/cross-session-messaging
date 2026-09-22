# Compress corpus.txt

{PEERS}Nobody assigns work and nobody ranks you. Choose your own next experiment.

## The problem

`corpus.txt` is 81 KB of Markdown, mixed Korean and English (UTF-8). Write a compressor for it.

A candidate is one Python file in `candidates/` defining `compress(data: bytes) -> bytes` and
`decompress(blob: bytes) -> bytes`. `python3 eval.py candidates/<file>.py` checks that
`decompress(compress(corpus))` gives the corpus back and prints
`{"ok": true, "score": ..., "compressed": ..., "code": ...}`.

**score = compressed size + the size of your source file. Lower is better.** Counting the
source means hiding the corpus in the code does not pay.

Rules the evaluator enforces: imports only from math, collections, heapq, itertools,
functools, struct, array, bisect, operator (so no zlib, lzma, bz2 — write the compression
yourself); no open, exec, eval, compile, or double-underscore names; compress plus decompress
within 20 seconds. The starting point, `baseline.py`, stores the corpus as it is.

There are many ways in — entropy coding, dictionaries, match finding, transforms, context
models, anything about the corpus itself. None is known to be best here.

## The shared record

A graph of notes in `notes.md`, read and written only through `./xsm doc`:

    ./xsm doc log notes.md                 everything so far, oldest first
    ./xsm doc leaves notes.md              nodes nothing builds on yet
    ./xsm doc show notes.md <id>           one node in full
    ./xsm doc add notes.md --parent <id> --tag result --text "..."

Tags: `result` (an experiment and its measured score — failures too), `insight` (a pattern
you noticed, parents = the evidence), `hypothesis` (an idea you have not tested),
`verification` (you re-ran someone else's result; parent = that node, say what you got).
A node can have several `--parent`s.

## The loop

Repeat until `./timeleft` prints 0:

1. Read the record (`log`, `leaves`).
2. Pick a parent node to build on.
3. Write a candidate in `candidates/<a-new-name>.py`, making one change from the parent's code.
4. `python3 eval.py candidates/<a-new-name>.py`
5. Publish a node: the parent id, the file, the one change, the measured score.
6. Go back to 1.

Write only inside this folder.
{CHANNEL}{RULES}