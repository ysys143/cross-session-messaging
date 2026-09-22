"""Who sent what, and whether it actually arrived.

The send call cannot answer the second question. agent-comms reports "Sent"
for a message its recipient never sees, and nothing in the sender's own
result distinguishes the two (S9 E6-3). So the sender writes `queued`, the
receiving hook writes a receipt, and only a receipt turns a message into
`delivered`.
"""
from __future__ import annotations

import os
import time

from . import paths


def _entry_path(msg_id: str) -> str:
    return paths.path(paths.LEDGER, "%s.json" % msg_id)


def _receipt_path(msg_id: str) -> str:
    return paths.path(paths.LEDGER, "%s.recv.json" % msg_id)


def queued(msg_id: str, sender: dict, target: dict, scope: str, kind: str, body: str) -> dict:
    entry = {"id": msg_id, "status": "queued", "t": time.time(), "kind": kind, "scope": scope,
             "from": {k: sender.get(k) for k in ("name", "alias", "ref", "runtime")},
             "to": {k: target.get(k) for k in ("name", "alias", "ref", "runtime")},
             "preview": body[:200]}
    paths.write_json(_entry_path(msg_id), entry)
    return entry


def failed(msg_id: str, reason: str) -> None:
    """The send never left: no receipt will come, so the entry itself says so.
    (Measured 2026-09-22: five sends blocked by a sandbox sat as `queued`.)"""
    entry = paths.read_json(_entry_path(msg_id), {}) or {}
    entry.update({"status": "error", "error": reason, "failed_t": time.time()})
    paths.write_json(_entry_path(msg_id), entry)


def receipt(msg_id: str, decision: str, receiver: dict | None, reason: str = "",
            outcome: str | None = None) -> None:
    """Written by the receiving hook. `decision` is delivered | held | blocked."""
    rec = {"id": msg_id, "decision": decision, "reason": reason, "t": time.time(),
           "receiver": {k: (receiver or {}).get(k) for k in ("name", "alias", "ref", "runtime")}}
    if outcome:
        rec["outcome"] = outcome
    paths.write_json(_receipt_path(msg_id), rec)


def close(task_id: str, outcome: str, by: dict | None = None) -> None:
    """How the task sent as `task_id` ended, written on the task's own entry.

    The reply that carries the outcome is a message of its own and its receipt
    goes with it; a `--once` worker is stopped and forgotten moments later, so
    the entry the sender already holds the id of is the only lasting place for
    this. Left on a separate key because `status()` overwrites `status` with
    the receipt's decision."""
    entry = paths.read_json(_entry_path(task_id), {}) or {}
    if not entry:
        return                      # nothing sent under that id here; nothing to close
    entry["outcome"] = outcome
    entry["closed_t"] = time.time()
    if by:
        entry["closed_by"] = {k: by.get(k) for k in ("name", "alias", "ref", "runtime")}
    paths.write_json(_entry_path(task_id), entry)


def received(msg_id: str) -> bool:
    """Whether some path already handed this message over (or refused it)."""
    return os.path.exists(_receipt_path(msg_id))


def status(msg_id: str) -> dict:
    entry = paths.read_json(_entry_path(msg_id), {}) or {}
    rec = paths.read_json(_receipt_path(msg_id))
    if rec:
        entry["status"] = rec.get("decision")
        entry["receipt"] = rec
        if rec.get("outcome") and not entry.get("outcome"):
            # A reply's own outcome belongs on the reply; a task's comes from
            # close(). Neither overwrites the other.
            entry["outcome"] = rec["outcome"]
    return entry


def wait_for(msg_id: str, seconds: float, interval: float = 0.4) -> dict:
    deadline = time.time() + seconds
    while True:
        state = status(msg_id)
        if state.get("status") != "queued":
            return state
        if time.time() >= deadline:
            return state
        time.sleep(interval)


def recent(limit: int = 20) -> list:
    entries = []
    try:
        names = os.listdir(paths.path(paths.LEDGER))
    except OSError:
        return []
    for name in names:
        if name.endswith(".recv.json") or not name.endswith(".json"):
            continue
        entries.append(status(name[:-len(".json")]))
    return sorted(entries, key=lambda e: e.get("t", 0), reverse=True)[:limit]
