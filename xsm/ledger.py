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


def receipt(msg_id: str, decision: str, receiver: dict | None, reason: str = "") -> None:
    """Written by the receiving hook. `decision` is delivered | held | blocked."""
    paths.write_json(_receipt_path(msg_id), {
        "id": msg_id, "decision": decision, "reason": reason, "t": time.time(),
        "receiver": {k: (receiver or {}).get(k) for k in ("name", "alias", "ref", "runtime")}})


def status(msg_id: str) -> dict:
    entry = paths.read_json(_entry_path(msg_id), {}) or {}
    rec = paths.read_json(_receipt_path(msg_id))
    if rec:
        entry["status"] = rec.get("decision")
        entry["receipt"] = rec
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
