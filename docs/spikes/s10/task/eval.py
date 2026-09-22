#!/usr/bin/env python3
"""Score a candidate compressor on corpus.txt.

    python3 eval.py candidates/mine.py   ->   {"ok": true, "score": 41203, ...}

score = len(compress(corpus)) + len(the candidate's source), lower is better.
Counting the source means storing the corpus in the decompressor does not pay.

A candidate is one Python file defining compress(data: bytes) -> bytes and
decompress(blob: bytes) -> bytes. It must be pure computation: it may import
only the modules in ALLOWED, and may not call open/exec/eval/compile/__import__
or similar — stdlib compressors (zlib, lzma, bz2, ...) are out, so is reading
the corpus back from disk. Both calls together must finish within LIMIT seconds.
"""
import ast
import importlib.util
import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIMIT = 20
ALLOWED = {"math", "collections", "heapq", "itertools", "functools", "struct", "array",
           "bisect", "operator"}
FORBIDDEN_CALLS = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
                   "globals", "locals", "vars", "memoryview"}


def check(source: str) -> str | None:
    """Why a candidate is not allowed, or None."""
    try:
        tree = ast.parse(source)
    except SyntaxError as err:
        return "syntax error: %s" % err
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]] if node.level == 0 else ["<relative>"]
        else:
            names = []
        bad = [n for n in names if n not in ALLOWED]
        if bad:
            return "import not allowed: %s (allowed: %s)" % (", ".join(bad), ", ".join(sorted(ALLOWED)))
        # __builtins__["open"] reaches open without naming it, and the other
        # dunder globals (__loader__, __spec__, __file__) lead back to the disk.
        if isinstance(node, ast.Name) and (node.id in FORBIDDEN_CALLS or node.id.startswith("__")):
            return "name not allowed: %s" % node.id
        if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr != "__init__":
            return "dunder attribute not allowed: %s" % node.attr
    return None


def load(path: str):
    spec = importlib.util.spec_from_file_location("candidate", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load %s as a Python module" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _timeout(_signum, _frame):
    raise TimeoutError


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 eval.py <candidate.py>", file=sys.stderr)
        return 2
    with open(sys.argv[1], "rb") as fh:
        raw = fh.read()
    why = check(raw.decode("utf-8", errors="replace"))
    if why:
        print(json.dumps({"ok": False, "reason": why}))
        return 1
    with open(os.path.join(HERE, "corpus.txt"), "rb") as fh:
        corpus = fh.read()
    mod = load(sys.argv[1])
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(LIMIT)
    start = time.perf_counter()
    try:
        blob = mod.compress(corpus)
        back = mod.decompress(blob)
    except TimeoutError:
        print(json.dumps({"ok": False, "reason": "over %ds" % LIMIT}))
        return 1
    except Exception as err:                    # noqa: BLE001 - report any failure as data
        print(json.dumps({"ok": False, "reason": "%s: %s" % (type(err).__name__, err)}))
        return 1
    finally:
        signal.alarm(0)
    took = time.perf_counter() - start
    if not isinstance(blob, (bytes, bytearray)) or back != corpus:
        print(json.dumps({"ok": False, "reason": "decompress(compress(corpus)) != corpus"}))
        return 1
    print(json.dumps({"ok": True, "score": len(blob) + len(raw), "compressed": len(blob),
                      "code": len(raw), "seconds": round(took, 2)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
