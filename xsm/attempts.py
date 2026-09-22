"""How many times the same task has been handed to a worker, and how it went.

A session that gets a failure back can send the same task again, and nothing
stopped it: `spawn` had no memory, so three sessions in a row could burn the
same work. This keeps one small file per task so `spawn` can refuse the fourth
attempt at something that has failed three times.

The file lives outside the worker records on purpose — `stop()` deletes those,
and the whole point here is what survives the worker. There is no timer and no
process: an attempt is opened by `spawn` and closed by whatever happens next
(a reply, or `stop`), both of which already run.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import time

from . import paths

ATTEMPTS = "attempts"
KEEP = 20                   # a lineage is a handful of tries; keep the recent ones
KEY_LEN = 12
SPACE_RE = re.compile(r"\s+")


class AttemptsError(Exception):
    pass


def _normalize(text: str) -> str:
    """Whitespace and case are not what makes two tasks the same task."""
    return SPACE_RE.sub(" ", (text or "").strip()).lower()


def key_for(text: str, cwd: str) -> str:
    """The lineage a task belongs to. The folder is part of it: the same words
    in two repositories are two jobs."""
    where = os.path.realpath(os.path.expanduser(cwd or "."))
    digest = hashlib.sha256(("%s|%s" % (_normalize(text), where)).encode("utf-8")).hexdigest()
    return digest[:KEY_LEN]


def _path(key: str) -> str:
    return paths.path(ATTEMPTS, "%s.json" % key)


def read(key: str) -> dict:
    return paths.read_json(_path(key), {}) or {}


def all_records() -> list:
    out = [paths.read_json(p, {}) or {} for p in glob.glob(paths.path(ATTEMPTS, "*.json"))]
    return sorted([r for r in out if r.get("key")],
                  key=lambda r: (r.get("tries") or [{}])[-1].get("t", 0), reverse=True)


def key_of_task(task_id: str) -> str | None:
    """The lineage a task id belongs to, for `--retry-of`: a retry worded
    differently is still the same job, and saying which try it follows is how
    the caller says so."""
    for rec in all_records():
        if any(t.get("task_id") == task_id for t in rec.get("tries") or []):
            return rec["key"]
    return None


def failures(key: str) -> int:
    """Failures since the last success, newest first.

    An attempt that ended without saying how does not count and does not clear
    the count: xsm never reads a worker's prose to decide whether it worked."""
    count = 0
    for try_ in reversed(read(key).get("tries") or []):
        if try_.get("outcome") == "succeeded":
            return 0
        if try_.get("outcome") == "failed":
            count += 1
    return count


def start(key: str, text: str, cwd: str, worker: str, task_id: str | None) -> dict:
    rec = read(key) or {"key": key, "text": (text or "")[:200], "cwd": cwd, "first_t": time.time(),
                        "tries": []}
    rec["tries"] = (rec.get("tries") or [])[-(KEEP - 1):]
    rec["tries"].append({"t": time.time(), "worker": worker, "task_id": task_id})
    paths.write_json(_path(key), rec)
    return rec


def finish(key: str, outcome: str | None, why: str = "", task_id: str | None = None) -> None:
    """Close the most recent open try. Idempotent: the first close wins, so a
    `stop` that follows a reply cannot paint over what the reply said."""
    rec = read(key)
    tries = rec.get("tries") or []
    for try_ in reversed(tries):
        if try_.get("ended_t"):
            continue
        if task_id and try_.get("task_id") and try_["task_id"] != task_id:
            continue
        try_["ended_t"] = time.time()
        try_["outcome"] = outcome
        if why:
            try_["why"] = why
        paths.write_json(_path(key), rec)
        return


def check(key: str, limit: int) -> None:
    """Raise if this lineage has failed `limit` times in a row since its last
    success. The message carries a stable token so a caller can branch on it
    without parsing prose."""
    failed = failures(key)
    if limit <= 0 or failed < limit:
        return
    rec = read(key)
    history = "; ".join(
        "%s %s" % (t.get("worker") or "?", t.get("outcome") or "no answer")
        for t in (rec.get("tries") or [])[-limit:])
    raise AttemptsError(
        "task-attempts-exhausted: this task has failed %d times in this folder (%s). "
        "Sending it again the same way gets the same answer. Tell your user what failed and "
        "what the worker said it needed; a person clears it with `xsm attempts clear %s`."
        % (failed, history, key))


def clear(key: str) -> bool:
    try:
        os.unlink(_path(key))
        return True
    except OSError:
        return False


def prune(cutoff: float, dry_run: bool = False) -> list:
    """Forget lineages nothing has touched since `cutoff`."""
    gone = []
    for rec in all_records():
        last = (rec.get("tries") or [{}])[-1].get("t") or rec.get("first_t") or 0
        if last < cutoff:
            gone.append(rec["key"])
            if not dry_run:
                clear(rec["key"])
    return gone
