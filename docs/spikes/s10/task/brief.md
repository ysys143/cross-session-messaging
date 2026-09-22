# Make lcs_len fast

{PEERS}Nobody assigns work and nobody ranks you. Choose your own next experiment.

## The problem

`lcs_len(a, b)` returns the length of the longest common subsequence of two strings.
`python3 eval.py <file.py>` checks a candidate against 30 fixed pairs and prints
`{"ok": true, "ms": ...}`. Lower ms is better; `ok` must be true or the result does not count.
The starting point is `lcs_baseline.py`. `lcs_len` must compute its answer from its inputs on
every call: remembering answers across calls, in memory or on disk, does not count.

## The shared record

A graph of notes in `notes.md`, read and written only through `./xsm doc`:

    ./xsm doc log notes.md                 everything so far, oldest first
    ./xsm doc leaves notes.md              nodes nothing builds on yet
    ./xsm doc show notes.md <id>           one node in full
    ./xsm doc add notes.md --parent <id> --tag result --text "..."

Tags: `result` (an experiment and its measured ms — failures too), `insight` (a pattern
you noticed, parents = the evidence), `hypothesis` (an idea you have not tested),
`verification` (you re-ran someone else's result; parent = that node, say what you got).
A node can have several `--parent`s.

## The loop

Repeat until `./timeleft` prints 0:

1. Read the record (`log`, `leaves`).
2. Pick a parent node to build on.
3. Write a candidate in `candidates/<a-new-name>.py`, making one change from the parent's code.
4. `python3 eval.py candidates/<a-new-name>.py`
5. Publish a node: the parent id, the one change, the measured ms.
6. Go back to 1.

Write only inside this folder.
{CHANNEL}{RULES}