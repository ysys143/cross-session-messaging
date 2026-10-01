"""A message refused for want of a connection, kept until its person says yes.

`xsm send` to a session outside this one's scope was refused and forgotten: the
agent had to remember the text, ask, link, and send again, and when it forgot a
step the message never went (2026-10-01). The refusal now keeps the message
here, under the id it will be sent with, so the one yes that connects the
folders also sends it (connect.py). It is kept for TTL, because a person may
take a while to answer, and housekeeping sweeps what nobody came back for.

Not the ledger: nothing was queued, so there is no row to resend, and
`--resend` checks a 200-character preview, never the text. Not the held list:
that is what a receiver's gate refused.
"""
from __future__ import annotations

import os
import time

from . import consent, envelope, paths

OUTBOX = "outbox"
TTL = 86400.0


def _path(msg_id: str) -> str:
    return paths.path(OUTBOX, "%s.json" % os.path.basename(str(msg_id)))


def _mine(rec: dict, sender: dict) -> bool:
    """Whether the sender is the session that held it (a ref is 24 bits, so the
    session id counts when both sides have one)."""
    if not sender.get("ref") or rec.get("ref") != sender.get("ref"):
        return False
    return not (rec.get("session_id") and sender.get("session_id")) or \
        str(rec["session_id"]) == str(sender["session_id"])


def _records() -> list:
    try:
        names = sorted(os.listdir(paths.path(OUTBOX)))
    except OSError:
        return []
    found = []
    for name in names:
        rec = paths.read_json(os.path.join(paths.path(OUTBOX), name))
        if name.endswith(".json") and isinstance(rec, dict) and rec.get("id"):
            found.append(rec)
    return found


def _fresh(rec: dict, now: float | None = None) -> bool:
    return (time.time() if now is None else now) - float(rec.get("t") or 0) <= TTL


def put(sender: dict, spec: str, msg: dict) -> dict | None:
    """Keep `msg` ({body, kind, reply_to, outcome, priority}) for `spec`, or find
    it already kept: the same text to the same target from the same session is
    one held message however many times the refused command is run. None when
    it cannot be written, and the caller falls back to refusing."""
    key = consent.digest(spec, msg.get("kind"), msg.get("reply_to"), msg.get("outcome"),
                         msg.get("body"))
    for rec in _records():
        if rec.get("digest") == key and _mine(rec, sender) and _fresh(rec):
            return rec
    rec = dict(msg, id=envelope.new_id(), t=time.time(), digest=key, spec=spec,
               ref=sender.get("ref"), session_id=str(sender.get("session_id") or ""))
    try:
        paths.write_json(_path(rec["id"]), rec, mode=0o600)
    except OSError:
        return None
    return rec


def get(msg_id: str, sender: dict) -> dict | None:
    """The message held under this id by this session, or None."""
    rec = paths.read_json(_path(msg_id))
    return rec if isinstance(rec, dict) and _mine(rec, sender) and _fresh(rec) else None


def drop(msg_id: str) -> None:
    try:
        os.unlink(_path(msg_id))
    except OSError:
        pass


def prune(now: float | None = None, dry_run: bool = False) -> list:
    """Remove what outlived TTL; the names removed (or that would be)."""
    now = time.time() if now is None else now
    gone = [rec["id"] for rec in _records() if not _fresh(rec, now)]
    if not dry_run:
        for msg_id in gone:
            drop(msg_id)
    return gone
