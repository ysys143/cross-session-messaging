"""A copy of each message sent to a Codex session, for it to read mid-turn.

Codex takes its queue only between turns. A session that keeps working —
`sleep; ./phase; ./xsm channel` in a loop, which is what a Codex worker told
to check often does — never ends a turn and never receives anything: in S10
collab run 4 all six messages to the Codex worker (three phase announcements,
a peer's reply) sat in its queue for the whole fifteen minutes.

So the send side also keeps the envelope here, under the target's session id,
and `xsm inbox` (or the xsm_inbox MCP tool) hands it over through the same
checks the hook runs. Whichever reads it first writes the receipt; the other
path then sees the receipt and drops its copy — the hook refuses the queued
duplicate, which in Codex consumes it without a trace (S6).
"""
from __future__ import annotations

import os
import time

from . import paths

INBOX = "inbox"


def _dir(session_id: str) -> str:
    return paths.path(INBOX, str(session_id))


def keep(session_id: str, msg_id: str, content: str) -> None:
    paths.write_json(os.path.join(_dir(session_id), "%s.json" % msg_id),
                     {"id": msg_id, "t": time.time(), "content": content})


def drop(session_id: str | None, msg_id: str | None) -> None:
    if not (session_id and msg_id):
        return
    try:
        os.unlink(os.path.join(_dir(session_id), "%s.json" % msg_id))
    except OSError:
        pass


def count(session_id: str | None) -> int:
    """Cheap: one listdir. Runs after every command a Codex session makes."""
    if not session_id:
        return 0
    try:
        return sum(1 for n in os.listdir(_dir(session_id)) if n.endswith(".json"))
    except OSError:
        return 0


def take(session_id: str) -> list:
    """Claim every waiting copy, oldest first. The rename is the claim: two
    readers at once (a shell `xsm inbox` and the MCP tool) get each message
    once between them."""
    d = _dir(session_id)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return []
    out = []
    for name in names:
        claimed = os.path.join(d, name[:-len(".json")] + ".taking")
        try:
            os.rename(os.path.join(d, name), claimed)
        except OSError:
            continue
        item = paths.read_json(claimed)
        try:
            os.unlink(claimed)
        except OSError:
            pass
        if item and item.get("content"):
            out.append(item)
    return sorted(out, key=lambda i: i.get("t", 0))


def notice(session_id: str | None) -> str:
    n = count(session_id)
    if not n:
        return ""
    return ("[xsm] %d message%s from other sessions waiting for you; read with `xsm inbox` "
            "(or the xsm_inbox MCP tool)." % (n, "" if n == 1 else "s"))
